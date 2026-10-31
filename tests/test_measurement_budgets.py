"""Measurement sweep complexity and whole-query budgets through synthetic inputs."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from derived_records import BAND, PHONE, T0, manual, point, snapshot, store, summary
from interpretation_records import QUERY, SCOPE_ERROR, call
from interpretation_records import production_reader as production_reader

from app.parsers.common import canonical_json, payload_hash
from app.services import health_interpretation
from app.services import interpretation_measures as measures
from app.services.codex_health import ArchiveQuery
from app.services.interpretation_inputs import MAX_INPUT_BYTES, MAX_INPUT_RECORDS, Inputs
from app.services.manual_flags import record_flag


def amount(kind, name, start, end, value, source=BAND):
    return point(
        kind,
        str(name),
        {
            "interval": {
                "startTime": (T0 + timedelta(seconds=start)).isoformat(),
                "endTime": (T0 + timedelta(seconds=end)).isoformat(),
            },
            "millimeters" if kind == "distance" else "kcal": value,
        },
        source,
    )


def assert_refusal(result):
    assert result["schema_version"] == "2" and result["code"] == "summary_budget_exceeded"
    assert result["reason_codes"] == ["summary_budget_exceeded"]
    assert result["error"] == SCOPE_ERROR
    assert "summaries" not in result and "next_cursor" not in result


@pytest.mark.parametrize("kind", ["active-energy-burned", "total-calories", "distance"])
def test_fifty_thousand_disjoint_amounts_use_bounded_comparisons(monkeypatch, kind):
    count, comparisons = 50_000, 0

    class Stamp(str):
        def __lt__(self, other):
            nonlocal comparisons
            comparisons += 1
            assert comparisons < 30 * count  # Abort a quadratic regression deterministically.
            return super().__lt__(other)

        def __gt__(self, other):
            nonlocal comparisons
            comparisons += 1
            assert comparisons < 30 * count
            return super().__gt__(other)

    original = measures.evidence

    def evidence(*args, **kwargs):
        row = original(*args, **kwargs)
        return {**row, "start": Stamp(row["start"]), "end": Stamp(row["end"])}

    monkeypatch.setattr(measures, "evidence", evidence)
    rows = [
        SimpleNamespace(
            data_type=kind,
            raw_json=amount(kind, i, i, i + 0.5, 1000),
            source_start_time=T0 + timedelta(seconds=i),
            source_end_time=T0 + timedelta(seconds=i + 0.5),
            google_data_point_id=str(i),
            source_timezone="UTC",
        )
        for i in reversed(range(count))
    ]
    query = ArchiveQuery(action="longitudinal", data_type=kind, **QUERY)
    assert len(rows) == MAX_INPUT_RECORDS
    assert sum(len(canonical_json(row.raw_json).encode()) for row in rows) <= MAX_INPUT_BYTES
    result = measures.measurement_summaries(Inputs(rows, [], [], {}, ""), query, max_summaries=1)
    assert len(result) == 1 and result[0].statistics.count == count
    assert len(result[0].evidence) == count
    assert "overlapping_intervals" not in result[0].reason_codes
    assert 0 < comparisons < 30 * count


@pytest.mark.parametrize("kind", ["active-energy-burned", "total-calories", "distance"])
@pytest.mark.parametrize("overlap", [False, True])
async def test_touching_duplicates_and_conflicts_keep_original_amounts(sessions, kind, overlap):
    # Conflicting readings overlap the touching pair, but cannot contribute statistics.
    spans = [(-1800, 1800, 1000), (1800, 5400, 2000), (1800, 5400, 2000)]
    spans += [(3600, 7200, 3000), (3600, 7200, 4000)]
    if overlap:
        spans += [(900, 4500, 6000)]
    records = [amount(kind, i, *span) for i, span in enumerate(spans)]
    await store(sessions, kind, *records)
    async with sessions() as session:
        before = await snapshot(session)
    row = (await call("longitudinal", data_type=kind))["summaries"][0]
    factor = 0.001 if kind == "distance" else 1
    assert row["statistics"]["count"] == (3 if overlap else 2)
    assert row["statistics"]["maximum"] == (6000 if overlap else 2000) * factor
    assert row["statistics"]["minimum"] == 1000 * factor
    assert row["statistics"]["median"] == (2000 if overlap else 1500) * factor
    assert row["change"] is None and row["interval_seconds"] == 3600
    assert row["reason_codes"] == sorted(
        [
            "conflicting_values_at_same_time",
            "method_unspecified",
            "source_intervals_not_prorated_to_window",
            "variable_interval_scope_no_daily_total",
        ]
        + (["overlapping_intervals"] if overlap else [])
    )
    assert {e["record"] for e in row["evidence"]} == {r["name"] for r in records}
    assert sorted(e["value"] for e in row["evidence"]) == sorted(v * factor for _, _, v in spans)
    assert row["evidence"][0]["start"] == (T0 - timedelta(seconds=1800)).isoformat()
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize(
    "action,kind",
    [
        ("physiology", "daily-respiratory-rate"),
        ("longitudinal", "weight"),
        ("longitudinal", "body-fat"),
    ],
)
async def test_filtered_groups_and_absence_budget_precede_statistics_and_models(
    sessions, monkeypatch, action, kind
):
    for i, source in enumerate((BAND, PHONE)):
        for method in ("A", "B"):
            fields = dict(measurementMethod=method)
            if action == "physiology":
                fields.update(breathsPerMinute=12, fullSleepStats={"breathsPerMinute": 14})
                record = summary(kind, f"{i}-{method}", source=source, **fields)
            else:
                fields.update(sampleTime={"physicalTime": T0.isoformat()})
                fields["percentage" if kind == "body-fat" else "weightGrams"] = 70000
                record = point(kind, f"{i}-{method}", fields, source)
            await store(sessions, kind, record)
    if action == "longitudinal":
        await manual(sessions, 70, metric=kind.replace("-", "_"))
        flagged = await manual(sessions, 999, metric="muscle")
        async with sessions.begin() as session:
            await record_flag(session, flagged, "test", True)
    unsupported = {"date": QUERY["start_date"], "sampleTime": {"physicalTime": T0.isoformat()}}
    await store(sessions, kind, point(kind, "unsupported", unsupported, PHONE))
    whole = (await call(action))["summaries"]
    assert len(whole) == (15 if action == "physiology" else 11)
    selected = next(row for row in whole if row["state"] == "available")
    missing = next(row for row in whole if row["state"] == "absent")
    filters = [
        {},
        {"source": "manuel" if action == "longitudinal" else selected["source"]},
        {"definition": selected["definition"]},
        {key: selected[key] for key in ("source", "definition")},
        {"source": "unknown"},
        {"definition": missing["definition"]},
        {"source": "missing"},
        {"source": "unknown", "definition": kind},
    ]
    async with sessions() as session:
        before = await snapshot(session)
    for selection in filters:
        expected = [r for r in whole if all(r[k] == v for k, v in selection.items())]
        monkeypatch.setattr(health_interpretation, "MAX_SUMMARIES", len(expected))
        page = await call(action, limit=1, **selection)
        assert page["total_summaries"] == len(expected)
        rest = await call(action, cursor=page["next_cursor"], limit=200, **selection)
        assert page["summaries"] + (rest["summaries"] if page["next_cursor"] else []) == expected
        with monkeypatch.context() as patch:
            blocked = Mock(side_effect=AssertionError("budget must precede statistics and models"))
            for name in ("statistics", "Longitudinal", "_overlapping_intervals"):
                patch.setattr(measures, name, blocked)
            if expected:
                patch.setattr(health_interpretation, "MAX_SUMMARIES", len(expected) - 1)
                for paging in ({}, {"limit": 1}, {"cursor": page["next_cursor"]}):
                    assert_refusal(await call(action, **paging, **selection))
            else:
                assert (await call(action, **selection))["summaries"] == []
            blocked.assert_not_called()
    async with sessions() as session:
        assert await snapshot(session) == before
    monkeypatch.setattr(health_interpretation, "MAX_SUMMARIES", len(whole))
    page = await call(action, source=selected["source"], limit=1)
    assert page["next_cursor"]
    outside = PHONE if selected["source"] == "kaynak-" + payload_hash(BAND) else BAND
    await store(sessions, kind, point(kind, "outside", unsupported, outside))
    changed = await call(action, source=selected["source"], cursor=page["next_cursor"])
    assert changed["code"] == "stale_cursor"
    assert (await call(action, source=selected["source"], limit=1))["summaries"] == page[
        "summaries"
    ]


@pytest.mark.parametrize(
    "action,kind",
    [
        ("physiology", "core-body-temperature"),
        ("longitudinal", "muscle"),
        ("longitudinal", "body-fat"),
    ],
)
@pytest.mark.parametrize("present", [None, False, True])
async def test_single_measurement_or_absence_uses_budget_and_filters(
    sessions, monkeypatch, action, kind, present
):
    if present is not None and action == "longitudinal":
        marked = await manual(sessions, 70, metric=kind.replace("-", "_"))
        async with sessions.begin() as session:
            await record_flag(session, marked, "test", True)
            await record_flag(session, marked, "test", not present)
    present = bool(present) and action == "longitudinal"
    args = dict(data_type=kind)
    monkeypatch.setattr(health_interpretation, "MAX_SUMMARIES", 1)
    row = (await call(action, **args))["summaries"][0]
    assert row["state"] == ("available" if present else "absent")
    assert row["metric"] == (kind.replace("-", "_") if present else kind)
    assert row["statistics"]["count"] == (1 if present else 0)
    if present:
        assert row["statistics"]["median"] == row["evidence"][0]["value"] == 70
        assert row["source"] == "manuel" and row["definition"] == "manual_measurement"
    monkeypatch.setattr(health_interpretation, "MAX_SUMMARIES", 0)
    blocked = Mock(side_effect=AssertionError("absent summaries also require preflight"))
    monkeypatch.setattr(measures, "statistics", blocked)
    monkeypatch.setattr(measures, "Longitudinal", blocked)
    assert_refusal(await call(action, limit=1, **args))
    source = "unknown" if present else "manuel"
    assert (await call(action, source=source, **args))["summaries"] == []
    definition = kind if present else "missing"
    assert (await call(action, definition=definition, **args))["summaries"] == []
    blocked.assert_not_called()
