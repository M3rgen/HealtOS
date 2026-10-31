"""Relationship preflight boundaries through the production reader and archive."""

from collections import Counter
from datetime import timedelta
from unittest.mock import Mock

import pytest
from derived_records import BAND, DAY, PHONE, snapshot, store, summary
from interpretation_records import SCOPE_ERROR, call
from interpretation_records import production_reader as production_reader

from app.parsers.common import canonical_json
from app.services import health_interpretation as reader
from app.services import interpretation_relationship_plan as planning
from app.services import interpretation_stats as stats
from app.services.codex_health import ArchiveQuery
from app.services.interpretation_inputs import MAX_INPUT_BYTES, MAX_INPUT_RECORDS, BudgetError
from app.services.interpretation_types import Refusal

KIND = "daily-resting-heart-rate"


def query(**kwargs):
    return dict(metric="resting_bpm", other_metric="resting_bpm", **kwargs)


async def archive(sessions, *, sources=(BAND, PHONE), methods=("A", "B"), days=3, copies=2):
    first = DAY - timedelta(days=days - 1)
    records = [
        summary(
            KIND,
            f"{s}-{method}-{i}-{j}",
            day=first + timedelta(days=i),
            source=source,
            beatsPerMinute=50 + i,
            measurementMethod=method,
        )
        for s, source in enumerate(sources)
        for method in methods
        for i in range(days)
        for j in range(copies)
    ]
    assert len(records) < MAX_INPUT_RECORDS
    assert sum(len(canonical_json(r).encode()) for r in records) < MAX_INPUT_BYTES
    await store(sessions, KIND, *records)
    return query(start_date=str(first), lags=[-1, 1])


def assert_refusal(result, code):
    assert Refusal.model_validate(result).code == code
    assert result["reason_codes"] == [code]
    if code.endswith("_budget_exceeded"):
        assert result["error"] == SCOPE_ERROR
    assert "summaries" not in result and "next_cursor" not in result


def block_generation(patch):
    blocked = Mock(side_effect=AssertionError("preflight must precede all output allocation"))
    patch.setattr(stats, "correlation", blocked)
    patch.setattr(stats, "Relationship", blocked)
    original = reader.daily_series

    class LengthOnly(list):
        def __iter__(self):
            return blocked()

        def __getitem__(self, index):
            return blocked()

        def copy(self):
            return blocked()

    def guarded(*args):
        series = original(*args)
        for days in series.values():
            for row in days.values():
                row["evidence"] = LengthOnly(row["evidence"])
        return series

    patch.setattr(reader, "daily_series", guarded)
    return blocked


@pytest.mark.parametrize(
    "budget,code",
    [
        ("MAX_SUMMARIES", "summary_budget_exceeded"),
        ("MAX_RELATIONSHIP_PAIRS", "relationship_pair_budget_exceeded"),
        ("MAX_RELATIONSHIP_REFERENCES", "relationship_reference_budget_exceeded"),
    ],
)
async def test_filtered_whole_query_budget_boundaries_precede_generation(
    sessions, monkeypatch, budget, code
):
    args = await archive(sessions)
    # Unrelated metrics consume none of the three relationship budgets.
    await store(
        sessions,
        "daily-oxygen-saturation",
        summary("daily-oxygen-saturation", averagePercentage=95),
    )
    whole = (await call("relationship", **args))["summaries"]
    chosen = whole[0]
    selections = [
        {},
        {"source": chosen["source"]},
        {"other_source": chosen["other_source"]},
        {"definition": chosen["definition"]},
        {"other_definition": chosen["other_definition"]},
        {k: chosen[k] for k in ("source", "other_source", "definition", "other_definition")},
        {"source": "absent"},
        {"other_definition": "absent"},
    ]
    async with sessions() as session:
        before = await snapshot(session)
    for selection in selections:
        expected = [r for r in whole if all(r[k] == v for k, v in selection.items())]
        pairs = [p for r in expected for p in r["pairs"]]
        references = [ref for p in pairs for side in ("x_records", "y_records") for ref in p[side]]
        count = {
            "MAX_SUMMARIES": len(expected),
            "MAX_RELATIONSHIP_PAIRS": len(pairs),
            "MAX_RELATIONSHIP_REFERENCES": len(references),
        }[budget]
        if pairs:
            assert len(references) > len(set(references))  # Every reuse counts.
        for ceiling in (count + 1, count):
            monkeypatch.setattr(reader, budget, ceiling)
            page = await call("relationship", limit=1, **args, **selection)
            assert page["total_summaries"] == len(expected)
            assert page["summaries"] == expected[:1]
            assert (await call("relationship", limit=200, **args, **selection))[
                "summaries"
            ] == expected
            if page["next_cursor"]:
                rest = await call(
                    "relationship", cursor=page["next_cursor"], limit=200, **args, **selection
                )
                assert page["summaries"] + rest["summaries"] == expected
        if count:
            with monkeypatch.context() as patch:
                patch.setattr(reader, budget, count - 1)
                blocked = block_generation(patch)
                for limit in (1, 200):
                    for cursor in (None, page["next_cursor"]):
                        assert_refusal(
                            await call(
                                "relationship", limit=limit, cursor=cursor, **args, **selection
                            ),
                            code,
                        )
                blocked.assert_not_called()
    async with sessions() as session:
        assert await snapshot(session) == before


async def test_sixteen_self_regimes_and_five_lags_refused_before_pair_planning(
    sessions, monkeypatch
):
    sources = [{**BAND, "device": {"displayName": f"Synthetic {i}"}} for i in range(16)]
    args = await archive(sessions, sources=sources, methods=("A",), days=1, copies=1)
    args["lags"] = [-2, -1, 0, 1, 2]
    assert 16 * 16 * 5 == 1280 > reader.MAX_SUMMARIES
    blocked = block_generation(monkeypatch)
    monkeypatch.setattr(planning, "RelationshipPlan", blocked)
    for limit in (1, 200):
        assert_refusal(await call("relationship", limit=limit, **args), "summary_budget_exceeded")
    blocked.assert_not_called()
    await store(
        sessions,
        KIND,
        summary(KIND, "seventeenth", beatsPerMinute=50, source={**BAND, "platform": "OTHER"}),
    )
    assert_refusal(await call("relationship", **args), "relationship_regime_budget_exceeded")
    blocked.assert_not_called()


@pytest.mark.parametrize("kind,regimes,copies", [("pair", 16, 1), ("reference", 12, 2)])
async def test_default_budgets_reject_evidence_amplification_below_input_limits(
    sessions, monkeypatch, kind, regimes, copies
):
    sources = [{**BAND, "device": {"displayName": f"Synthetic {i}"}} for i in range(regimes)]
    args = await archive(sessions, sources=sources, methods=("A",), days=90, copies=copies)
    args["lags"] = [0]
    pair_count = regimes * regimes * 90
    assert regimes * regimes < reader.MAX_SUMMARIES
    if kind == "pair":
        assert pair_count > reader.MAX_RELATIONSHIP_PAIRS
    else:
        assert pair_count < reader.MAX_RELATIONSHIP_PAIRS
        assert pair_count * 2 * copies > reader.MAX_RELATIONSHIP_REFERENCES
    async with sessions() as session:
        before = await snapshot(session)
    blocked = block_generation(monkeypatch)
    for limit in (1, 200):
        assert_refusal(
            await call("relationship", limit=limit, **args),
            f"relationship_{kind}_budget_exceeded",
        )
    blocked.assert_not_called()
    async with sessions() as session:
        assert await snapshot(session) == before


def test_default_pair_and_reference_limits_accept_exactly_the_ceiling():
    # 15 regimes, 134 dates, three lags: 10 * 5 * (134 + 133 + 133)
    # = 20,000 pairs and 40,000 references, from only 2,010 observations.
    first = DAY - timedelta(days=135)
    series = {
        (metric, str(i), "definition", "unit", "method", "civil_day"): {
            first + timedelta(days=j): dict(value=j, comparable=True, evidence=[{"record": str(j)}])
            for j in range(134)
        }
        for metric, count in (("resting_bpm", 10), ("hrv_ms", 5))
        for i in range(count)
    }
    request = ArchiveQuery(
        action="relationship",
        metric="resting_bpm",
        other_metric="hrv_ms",
        start_date=first,
        end_date=DAY,
        lags=[0, -1, 1],
    )
    budgets = dict(
        max_summaries=reader.MAX_SUMMARIES,
        max_pairs=reader.MAX_RELATIONSHIP_PAIRS,
        max_references=reader.MAX_RELATIONSHIP_REFERENCES,
    )
    plans = planning.relationship_plans(series, request, **budgets)
    assert len(plans) == 150
    pairs = sum(sum(1 for _ in p.matching_days()) for p in plans)
    assert pairs == budgets["max_pairs"] == 20_000
    assert 2 * pairs == budgets["max_references"] == 40_000
    for budget, code in (("max_pairs", "pair"), ("max_references", "reference")):
        with pytest.raises(BudgetError, match=f"^relationship_{code}_budget_exceeded$"):
            planning.relationship_plans(series, request, **{**budgets, budget: budgets[budget] - 1})


def test_date_span_with_only_gaps_still_consumes_one_summary(monkeypatch):
    key = ("resting_bpm", "source", "definition", "bpm", "method", "civil_day")
    series = {
        key: {
            day: dict(value=1, comparable=True, evidence=[{"record": str(day)}])
            for day in (DAY - timedelta(days=1), DAY + timedelta(days=1))
        },
        # This regime's span is entirely outside the requested calendar.
        (key[0], "outside", *key[2:]): {
            DAY - timedelta(days=2): dict(value=2, comparable=True, evidence=[])
        },
    }
    request = ArchiveQuery(action="relationship", start_date=DAY, end_date=DAY, **query())
    budgets = dict(max_summaries=1, max_pairs=0, max_references=0)
    rows = stats.relationships(series, request, **budgets)
    assert len(rows) == 1 and rows[0].pairs == [] and rows[0].support_pairs == 0
    blocked = Mock(side_effect=AssertionError("even empty summaries require preflight"))
    monkeypatch.setattr(stats, "correlation", blocked)
    monkeypatch.setattr(stats, "Relationship", blocked)
    with pytest.raises(BudgetError, match="^summary_budget_exceeded$"):
        stats.relationships(series, request, **{**budgets, "max_summaries": 0})
    blocked.assert_not_called()


async def test_dates_sorted_once_per_regime_and_cursor_tracks_unselected_inputs(
    sessions, monkeypatch
):
    args = await archive(sessions)
    args["lags"] = [-1, 0, 1]
    visits = Counter()
    original = reader.daily_series

    class Days(dict):
        def __iter__(self):
            visits["sorts"] += 1
            return super().__iter__()

        def items(self):
            raise AssertionError("do not sort days again for each combination")

    def counted(*args):
        series = original(*args)
        visits["regimes"] = len(series)
        return {key: Days(days) for key, days in series.items()}

    monkeypatch.setattr(reader, "daily_series", counted)
    page = await call("relationship", limit=1, **args)
    assert page["next_cursor"]
    assert visits["sorts"] == visits["regimes"] == 4
    selected = {
        key: page["summaries"][0][key]
        for key in ("source", "other_source", "definition", "other_definition")
    }
    page = await call("relationship", limit=1, **args, **selected)
    await store(
        sessions,
        "daily-oxygen-saturation",
        summary("daily-oxygen-saturation", averagePercentage=95),
    )
    assert_refusal(
        await call("relationship", cursor=page["next_cursor"], **args, **selected), "stale_cursor"
    )
    assert (await call("relationship", limit=1, **args, **selected))["summaries"] == page[
        "summaries"
    ]
    assert_refusal(
        await call("relationship", cursor=page["next_cursor"], **{**args, "lags": [0]}, **selected),
        "cursor_query_mismatch",
    )
