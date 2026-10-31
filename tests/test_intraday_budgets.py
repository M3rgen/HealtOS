"""Intraday sweep and whole-query limits through the production archive reader."""

import json
from collections import Counter
from datetime import datetime, timedelta
from itertools import pairwise
from random import Random
from unittest.mock import Mock

import pytest
from derived_records import BAND, DAY, PHONE, T0, point, snapshot, steps, store
from interpretation_records import SCOPE_ERROR, call
from interpretation_records import production_reader as production_reader
from sqlalchemy import select, update

from app.google_health.data_types import REGISTRY
from app.models.health import Steps
from app.models.identity import RawSourceDevice
from app.models.state import RawHealthData
from app.parsers.common import payload_hash
from app.services import health_interpretation
from app.services import interpretation_cells as cells_service
from app.services import interpretation_intraday as intraday
from app.services.interpretation_types import Interpretation, Refusal


def heart(name, offset, value):
    return point(
        "heart-rate",
        name,
        {
            "sampleTime": {"physicalTime": (T0 + timedelta(seconds=offset)).isoformat()},
            "beatsPerMinute": value,
        },
    )


def step(name, start, end, value, source=BAND, method=None):
    record = steps(
        name,
        start=T0 + timedelta(seconds=start),
        end=T0 + timedelta(seconds=end),
        count=value,
        source=source,
    )
    if method:
        record[REGISTRY["steps"].json_key]["measurementMethod"] = method
    return record


def assert_refusal(result, code):
    assert Refusal.model_validate(result).code == code
    assert result["reason_codes"] == [code]
    if code.endswith("_budget_exceeded"):
        assert result["error"] == SCOPE_ERROR
    assert "summaries" not in result and "next_cursor" not in result


@pytest.mark.parametrize("dense", [False, True])
@pytest.mark.parametrize("count", [128, 512])
async def test_interval_comparisons_and_event_visits_are_not_quadratic(
    sessions, monkeypatch, dense, count
):
    await store(
        sessions,
        "steps",
        *[
            step(
                str(i),
                i + 1 if dense else 2 * i + 1,
                2 * count + 1 - i if dense else 2 * i + 2,
                2 * (count - i) if dense else 1,
            )
            for i in range(count)
        ],
    )
    comparisons = visits = 0

    class Stamp(datetime):
        def __lt__(self, other):
            nonlocal comparisons
            comparisons += 1
            return super().__lt__(other)

        def __le__(self, other):
            nonlocal comparisons
            comparisons += 1
            return super().__le__(other)

        def __gt__(self, other):
            nonlocal comparisons
            comparisons += 1
            return super().__gt__(other)

        def __ge__(self, other):
            nonlocal comparisons
            comparisons += 1
            return super().__ge__(other)

    class Events(list):
        def __iter__(self):
            nonlocal visits
            for event in super().__iter__():
                visits += 1
                yield event

    original_utc, original_cells = intraday.as_utc, cells_service.CellPlan.cells

    def counted_utc(value):
        stamp = original_utc(value)
        return Stamp.fromisoformat(stamp.isoformat()) if stamp else None

    def counted_cells(plan):
        plan.events = {stamp: Events(events) for stamp, events in plan.events.items()}
        yield from original_cells(plan)

    monkeypatch.setattr(intraday, "as_utc", counted_utc)
    monkeypatch.setattr(cells_service.CellPlan, "cells", counted_cells)
    # Isolate computation from the independent JSON response-size limit.
    monkeypatch.setattr(health_interpretation, "RESPONSE_BUDGET_BYTES", 8 * 1024 * 1024)
    row = (await call("intraday", limit=1))["summaries"][0]
    assert len(row["cells"]) == 2 * count + 1
    assert len(row["evidence"]) == count
    assert row["supported_seconds"] == (2 if dense else count)
    assert row["conflict_seconds"] == (2 * count - 2 if dense else 0)
    assert row["time_weighted_mean"] == 1
    assert visits == 2 * count
    assert 0 < comparisons < 100 * count  # A per-cell full scan exceeds this bound.


def naive_cells(spans):
    left, stop = T0, T0 + timedelta(days=1)
    clipped = [
        (max(left, T0 + timedelta(seconds=a)), min(stop, T0 + timedelta(seconds=b)), v)
        for a, b, v in spans
    ]
    edges = sorted({left, stop, *(edge for a, b, _ in clipped for edge in (a, b))})
    result = []
    for a, b in pairwise(edges):
        active = [v for p, q, v in clipped if p < b and q > a]
        state = (
            "conflict"
            if len(active) > 1
            else "supported"
            if active and active[0] is not None
            else "gap"
        )
        result.append(
            dict(
                start=a.isoformat(),
                end=b.isoformat(),
                state=state,
                value=active[0] if state == "supported" else None,
            )
        )
    return result


async def test_step_cells_match_reference_with_equal_endpoints_missing_and_zero(sessions):
    spans = [(-2, 1, 3), (1, 4, 6), (1, 3, 8), (2, 3, None), (3, 5, 4), (5, 6, 0)]
    rng = Random(114)
    for _ in range(30):
        a = rng.randrange(10, 100)
        spans.append((a, a + rng.randrange(1, 20), rng.choice([None, 0, 12])))
    spans += [(110, 120, None), (120, 130, 0), (86399, 86402, 6)]
    await store(sessions, "steps", *[step(str(i), *span) for i, span in enumerate(spans)])
    async with sessions() as session:
        before = await snapshot(session)
    result = await call("intraday", data_type="steps")
    row = result["summaries"][0]
    expected = naive_cells([(a, b, v / (b - a) if v is not None else None) for a, b, v in spans])
    assert row["cells"] == expected
    durations = {state: 0.0 for state in ("supported", "gap", "conflict")}
    weighted = 0.0
    for cell in expected:
        seconds = (
            datetime.fromisoformat(cell["end"]) - datetime.fromisoformat(cell["start"])
        ).total_seconds()
        durations[cell["state"]] += seconds
        if cell["value"] is not None:
            weighted += cell["value"] * seconds
    for state, seconds in durations.items():
        assert row[state + "_seconds"] == seconds
    assert row["time_weighted_mean"] == weighted / durations["supported"]
    assert row["statistics"]["count"] == sum(v is not None for _, _, v in spans)
    assert Counter((e["start"], e["end"], e["value"]) for e in row["evidence"]) == Counter(
        ((T0 + timedelta(seconds=a)).isoformat(), (T0 + timedelta(seconds=b)).isoformat(), v)
        for a, b, v in spans
    )
    assert row["exact_bursts"] is None
    assert "exact_movement_bursts_unavailable" in row["reason_codes"]
    Interpretation.model_validate(result)
    async with sessions() as session:
        assert await snapshot(session) == before


async def test_heart_conflicts_gap_limit_zero_and_clipped_interpolation(sessions):
    samples = [
        (-60, 60),
        (60, 120),
        (180, 80),
        (180, 90),
        (240, 100),
        (540, 100),
        (841, 50),
        (900, None),
        (960, 0),
        (1020, 0),
        (86340, 60),
        (86460, 180),
    ]
    await store(
        sessions, "heart-rate", *[heart(str(i), *sample) for i, sample in enumerate(samples)]
    )
    async with sessions() as session:
        before = await snapshot(session)
    row = (await call("intraday", data_type="heart-rate"))["summaries"][0]
    assert row["cells"] == naive_cells(
        [(0, 60, 105), (240, 540, 100), (960, 1020, 0), (86340, 86400, 90)]
    )
    assert row["supported_seconds"] == 480 and row["gap_seconds"] == 86400 - 480
    assert row["conflict_seconds"] == 0 and row["time_weighted_mean"] == 86.875
    assert row["statistics"]["count"] == 9 and row["statistics"]["median"] == 80
    assert len(row["evidence"]) == len(samples)
    assert "conflicting_same_time_samples" in row["reason_codes"]
    assert "unsupported_or_missing_value" in row["reason_codes"]
    json.dumps(row, allow_nan=False)
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize(
    "owner,setting,maximum,code",
    [
        (cells_service, "MAX_INTRADAY_CELLS", 12, "intraday_cell_budget_exceeded"),
        (cells_service, "MAX_INTRADAY_EVIDENCE", 4, "intraday_evidence_budget_exceeded"),
        (health_interpretation, "MAX_SUMMARIES", 4, "summary_budget_exceeded"),
    ],
)
async def test_whole_query_exact_limit_and_one_over_precede_all_output_allocation(
    sessions, monkeypatch, owner, setting, maximum, code
):
    await store(
        sessions,
        "steps",
        *[
            step(f"{day}-{i}", day * 86400 + 1, day * 86400 + 2, 0, source)
            for day in range(2)
            for i, source in enumerate((BAND, PHONE))
        ],
    )
    args = dict(end_date=str(DAY + timedelta(days=1)))
    async with sessions() as session:
        before = await snapshot(session)
    monkeypatch.setattr(owner, setting, maximum)
    whole = await call("intraday", **args)
    assert whole["total_summaries"] == 4
    page = await call("intraday", limit=1, **args)
    rest = await call("intraday", limit=200, cursor=page["next_cursor"], **args)
    assert page["summaries"] + rest["summaries"] == whole["summaries"]
    assert sum(len(row["cells"]) for row in whole["summaries"]) == 12
    assert sum(len(row["evidence"]) for row in whole["summaries"]) == 4
    monkeypatch.setattr(owner, setting, maximum - 1)
    blocked = Mock(side_effect=AssertionError("preflight must precede output allocation"))
    monkeypatch.setattr(intraday, "Intraday", blocked)
    monkeypatch.setattr(intraday, "evidence", blocked)
    monkeypatch.setattr(intraday, "statistics", blocked)
    monkeypatch.setattr(cells_service.CellPlan, "cells", blocked)
    for limit in (1, 200):
        assert_refusal(await call("intraday", limit=limit, **args), code)
    assert_refusal(await call("intraday", cursor=page["next_cursor"], **args), code)
    blocked.assert_not_called()
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize("filter_kind", ["source", "definition", "both"])
async def test_filters_precede_planning_but_excluded_changes_expire_cursor(
    sessions, monkeypatch, filter_kind
):
    records = [
        step(f"{day}-{i}-{method}", day * 86400 + 1, day * 86400 + 2, 1, source, method)
        for day in range(2)
        for i, source in enumerate((BAND, PHONE))
        for method in ("A", "B")
    ]
    await store(sessions, "steps", *records)
    args = dict(end_date=str(DAY + timedelta(days=1)))
    whole = (await call("intraday", **args))["summaries"]
    selected = whole[0]
    filters = {
        key: selected[key] for key in ("source", "definition") if filter_kind in (key, "both")
    }
    expected = [row for row in whole if all(row[key] == value for key, value in filters.items())]
    monkeypatch.setattr(cells_service, "MAX_INTRADAY_EVIDENCE", len(expected))
    monkeypatch.setattr(cells_service, "MAX_INTRADAY_CELLS", 3 * len(expected))
    monkeypatch.setattr(health_interpretation, "MAX_SUMMARIES", len(expected))
    planner = Mock(wraps=intraday.CellPlan)
    monkeypatch.setattr(intraday, "CellPlan", planner)
    page = await call("intraday", limit=1, **args, **filters)
    assert planner.call_count == len(expected)
    rest = await call("intraday", cursor=page["next_cursor"], **args, **filters)
    assert page["summaries"] + rest["summaries"] == expected
    assert rest["next_cursor"] is None
    for key in ("source", "definition"):
        assert (await call("intraday", **args, **{key: "absent"}))["total_summaries"] == 0
    outside = next(row for row in whole if row not in expected)
    outside_id = outside["evidence"][0]["record"]
    original = next(record for record in records if record["name"] == outside_id)
    changed = {
        **original,
        REGISTRY["steps"].json_key: {**original[REGISTRY["steps"].json_key], "count": 99},
    }
    await store(sessions, "steps", changed)
    async with sessions() as session:
        before = await snapshot(session)
    assert_refusal(
        await call("intraday", cursor=page["next_cursor"], **args, **filters), "stale_cursor"
    )
    assert (await call("intraday", **args, **filters))["summaries"] == expected
    async with sessions() as session:
        assert await snapshot(session) == before


async def test_evidence_budget_counts_repeated_boundary_samples(sessions, monkeypatch):
    await store(sessions, "heart-rate", heart("before", 86340, 60), heart("after", 86460, 120))
    args = dict(end_date=str(DAY + timedelta(days=1)), data_type="heart-rate")
    monkeypatch.setattr(cells_service, "MAX_INTRADAY_EVIDENCE", 4)
    result = await call("intraday", **args)
    assert result["total_summaries"] == 2
    assert [len(row["evidence"]) for row in result["summaries"]] == [2, 2]
    monkeypatch.setattr(cells_service, "MAX_INTRADAY_EVIDENCE", 3)
    assert_refusal(await call("intraday", limit=1, **args), "intraday_evidence_budget_exceeded")


@pytest.mark.parametrize(
    "mutation", ["member", "source_deleted", "raw_quarantine", "normalized_quarantine", "identity"]
)
async def test_intraday_membership_eligibility_and_identity_expire_cursor(sessions, mutation):
    await store(sessions, "steps", step("a", 1, 2, 1), step("b", 1, 2, 2, PHONE))
    page = await call("intraday", limit=1)
    assert page["total_summaries"] == 2 and page["next_cursor"]
    if mutation == "member":
        await store(sessions, "steps", step("new", 3, 4, 1))
    else:
        async with sessions.begin() as session:
            row = await session.scalar(
                select(RawHealthData).where(
                    RawHealthData.google_data_point_id == "synthetic/steps/a"
                )
            )
            if mutation == "identity":
                session.add(
                    RawSourceDevice(
                        data_type="steps",
                        fingerprint=payload_hash(BAND),
                        device="e" * 64,
                        decided_at=T0,
                    )
                )
            elif mutation == "normalized_quarantine":
                await session.execute(
                    update(Steps)
                    .where(Steps.google_data_point_id == row.google_data_point_id)
                    .values(is_quarantined=True)
                )
            else:
                values = (
                    dict(source_deleted_at=T0)
                    if mutation == "source_deleted"
                    else dict(is_quarantined=True)
                )
                await session.execute(
                    update(RawHealthData).where(RawHealthData.id == row.id).values(**values)
                )
    async with sessions() as session:
        before = await snapshot(session)
    assert_refusal(await call("intraday", cursor=page["next_cursor"]), "stale_cursor")
    current = await call("intraday")
    assert current["total_summaries"] == (2 if mutation in {"member", "identity"} else 1)
    async with sessions() as session:
        assert await snapshot(session) == before


async def test_evidence_budget_counts_uninterpolated_samples(sessions, monkeypatch):
    # Many conflicting samples have only one gap cell, but still consume evidence.
    await store(sessions, "heart-rate", *[heart(str(i), 60, 60 + i % 2) for i in range(4)])
    args = dict(data_type="heart-rate", end_date=str(DAY))
    monkeypatch.setattr(cells_service, "MAX_INTRADAY_EVIDENCE", 4)
    row = (await call("intraday", **args))["summaries"][0]
    assert len(row["evidence"]) == 4 and len(row["cells"]) == 1
    assert row["supported_seconds"] == 0
    monkeypatch.setattr(cells_service, "MAX_INTRADAY_EVIDENCE", 3)
    assert_refusal(await call("intraday", limit=1, **args), "intraday_evidence_budget_exceeded")


async def test_default_evidence_limit_bounds_multi_day_amplification(sessions, monkeypatch):
    # Only 242 source records amplify to exactly 20,000 references over 90 days.
    await store(
        sessions,
        "steps",
        *[step(f"long-{i}", -89 * 86400, 86400, i + 1) for i in range(222)],
        *[step(f"short-{i}", 2 * i + 1, 2 * i + 2, 1) for i in range(20)],
    )
    assert cells_service.MAX_INTRADAY_EVIDENCE == 20_000
    args = dict(start_date=str(DAY - timedelta(days=89)), data_type="steps")
    monkeypatch.setattr(health_interpretation, "RESPONSE_BUDGET_BYTES", 8 * 1024 * 1024)
    build = Mock(wraps=intraday.Intraday)
    monkeypatch.setattr(intraday, "Intraday", build)
    result = await call("intraday", limit=1, **args)
    assert result["total_summaries"] == 90 and result["next_cursor"]
    assert sum(len(item.kwargs["evidence"]) for item in build.call_args_list) == 20_000
    await store(sessions, "steps", step("one-over", 60, 61, 1))
    async with sessions() as session:
        before = await snapshot(session)
    blocked = Mock(side_effect=AssertionError("budget must precede evidence and models"))
    monkeypatch.setattr(intraday, "Intraday", blocked)
    monkeypatch.setattr(intraday, "evidence", blocked)
    for limit in (1, 200):
        assert_refusal(
            await call("intraday", limit=limit, **args), "intraday_evidence_budget_exceeded"
        )
    blocked.assert_not_called()
    async with sessions() as session:
        assert await snapshot(session) == before


async def test_default_cell_limit_exact_and_one_new_boundary(sessions, monkeypatch):
    assert cells_service.MAX_INTRADAY_CELLS == 20_000
    await store(
        sessions,
        "steps",
        *[step(str(i), 2 * i, 2 * i + 1, 1) for i in range(10_000)],
    )
    monkeypatch.setattr(health_interpretation, "RESPONSE_BUDGET_BYTES", 16 * 1024 * 1024)
    row = (await call("intraday", limit=1))["summaries"][0]
    assert len(row["cells"]) == 20_000 and len(row["evidence"]) == 10_000
    assert row["supported_seconds"] == 10_000 and row["time_weighted_mean"] == 1
    # A touching interval introduces exactly one new edge, rather than two.
    await store(sessions, "steps", step("one-over", 19_999, 20_000, 1))
    async with sessions() as session:
        before = await snapshot(session)
    blocked = Mock(side_effect=AssertionError("cell budget must precede output allocation"))
    monkeypatch.setattr(intraday, "Intraday", blocked)
    monkeypatch.setattr(intraday, "evidence", blocked)
    monkeypatch.setattr(cells_service.CellPlan, "cells", blocked)
    for limit in (1, 200):
        assert_refusal(await call("intraday", limit=limit), "intraday_cell_budget_exceeded")
    blocked.assert_not_called()
    async with sessions() as session:
        assert await snapshot(session) == before
