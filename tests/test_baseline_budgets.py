"""Personal reference planning through the production reader and synthetic archive."""

from collections import Counter
from datetime import timedelta
from statistics import median, quantiles
from unittest.mock import Mock

import pytest
from derived_records import BAND, DAY, PHONE, T0, sleep, snapshot, steps, store, summary
from interpretation_records import SCOPE_ERROR, call
from interpretation_records import production_reader as production_reader

from app.parsers.common import canonical_json, payload_hash
from app.services import health_interpretation
from app.services import interpretation_baseline_index as indexing
from app.services import interpretation_stats as stats
from app.services.interpretation_inputs import MAX_INPUT_BYTES, MAX_INPUT_RECORDS
from app.services.interpretation_types import Interpretation, Refusal

KIND = "daily-oxygen-saturation"


async def resting(sessions, values, start=DAY, source=BAND, method=None):
    # Distinct sources and methods need distinct record identities as well.
    await store(
        sessions,
        "daily-resting-heart-rate",
        *[
            summary(
                "daily-resting-heart-rate",
                f"{payload_hash(source)}-{method}-{start}-{i}",
                day=start + timedelta(days=i),
                source=source,
                beatsPerMinute=value,
                **(
                    {"dailyRestingHeartRateMetadata": {"calculationMethod": method}}
                    if method
                    else {}
                ),
            )
            for i, value in enumerate(values)
        ],
    )


def assert_refusal(result, code):
    assert Refusal.model_validate(result).code == code
    assert result["reason_codes"] == [code]
    if code.endswith("_budget_exceeded"):
        assert result["error"] == SCOPE_ERROR
    assert "summaries" not in result and "next_cursor" not in result


def block_generation(patch):
    blocked = Mock(side_effect=AssertionError("preflight must precede output allocation"))
    for name in ("statistics", "Baseline"):
        patch.setattr(stats, name, blocked)
    original = health_interpretation.daily_series

    class Evidence(list):
        def __iter__(self):
            return blocked()

        def __radd__(self, other):
            return blocked()

    def guarded(*args):
        series = original(*args)
        for days in series.values():
            for row in days.values():
                row["evidence"] = Evidence(row["evidence"])
        return series

    patch.setattr(health_interpretation, "daily_series", guarded)
    return blocked


@pytest.mark.parametrize("shape", ["sources", "days"])
async def test_series_and_day_visits_are_linear_through_production_reader(
    sessions, monkeypatch, shape
):
    count = 2048 if shape == "sources" else 146
    records = [
        summary(
            KIND,
            str(i),
            day=DAY if shape == "sources" else DAY - timedelta(days=145 - i),
            source={**BAND, "device": {"displayName": f"Synthetic {i}"}}
            if shape == "sources"
            else BAND,
            averagePercentage=95,
        )
        for i in range(count)
    ]
    assert len(records) < MAX_INPUT_RECORDS
    assert sum(len(canonical_json(r).encode()) for r in records) < MAX_INPUT_BYTES
    await store(sessions, KIND, *records)
    visits = Counter()

    class Days(dict):
        def __iter__(self):
            for day in super().__iter__():
                visits["days"] += 1
                assert visits["days"] <= 6 * count
                yield day

        def items(self):
            for item in super().items():
                visits["days"] += 1
                assert visits["days"] <= 6 * count
                yield item

    class Series(dict):
        def items(self):
            for item in super().items():
                visits["series"] += 1
                assert visits["series"] <= 4 * count
                yield item

    original = health_interpretation.daily_series

    def counted(*args):
        return Series({key: Days(days) for key, days in original(*args).items()})

    monkeypatch.setattr(health_interpretation, "daily_series", counted)
    args = dict(start_date=str(DAY if shape == "sources" else DAY - timedelta(days=89)))
    if shape == "sources":
        # The normal 1000-summary ceiling rejects this valid input before any
        # statistics. Raise it only to exercise the method index at larger scale.
        with monkeypatch.context() as patch:
            blocked = block_generation(patch)
            for limit in (1, 200):
                visits.clear()
                assert_refusal(
                    await call("baseline", limit=limit, **args), "summary_budget_exceeded"
                )
            blocked.assert_not_called()
        monkeypatch.setattr(health_interpretation, "MAX_SUMMARIES", count)
    visits.clear()
    result = await call("baseline", limit=1, **args)
    assert result["total_summaries"] == (count if shape == "sources" else 90)
    assert result["next_cursor"] and result["summaries"][0]["statistics"]["count"] == (
        0 if shape == "sources" else 56
    )
    assert 0 < visits["series"] <= 4 * count
    assert 0 < visits["days"] <= 6 * count


@pytest.mark.parametrize("action", ["baseline", "analyze"])
async def test_filtered_summary_boundaries_precede_statistics_and_models(
    sessions, monkeypatch, action
):
    await resting(sessions, [50, 60, 70])
    await resting(sessions, [40, 50, 60], source=PHONE)
    await store(sessions, KIND, summary(KIND, averagePercentage=95))
    args = dict(end_date=str(DAY + timedelta(days=2)))
    whole = (await call(action, **args))["summaries"]
    chosen = next(r for r in whole if r["metric"] == "resting_bpm")
    filters = [
        {},
        {"metric": chosen["metric"]},
        {"source": chosen["source"]},
        {"definition": chosen["definition"]},
        {key: chosen[key] for key in ("metric", "source", "definition")},
        {"metric": "steps"},
        {"source": "absent"},
        {"definition": "absent"},
    ]
    async with sessions() as session:
        before = await snapshot(session)
    for selection in filters:
        expected = [r for r in whole if all(r[k] == v for k, v in selection.items())]
        for budget in (len(expected), len(expected) + 1):
            monkeypatch.setattr(health_interpretation, "MAX_SUMMARIES", budget)
            counted = Mock(wraps=stats.statistics)
            with monkeypatch.context() as patch:
                patch.setattr(stats, "statistics", counted)
                page = await call(action, limit=1, **args, **selection)
            assert counted.call_count == len(expected)
            assert page["total_summaries"] == len(expected)
            if page["next_cursor"]:
                rest = await call(
                    action, cursor=page["next_cursor"], limit=200, **args, **selection
                )
                assert page["summaries"] + rest["summaries"] == expected
            else:
                assert page["summaries"] == expected
        with monkeypatch.context() as patch:
            blocked = block_generation(patch)
            if expected:
                patch.setattr(health_interpretation, "MAX_SUMMARIES", len(expected) - 1)
                for limit in (1, 200):
                    assert_refusal(
                        await call(
                            action, limit=limit, cursor=page["next_cursor"], **args, **selection
                        ),
                        "summary_budget_exceeded",
                    )
            else:
                assert (await call(action, **args, **selection))["summaries"] == []
            blocked.assert_not_called()
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize("action", ["baseline", "analyze"])
async def test_repeated_history_evidence_budget_counts_full_filtered_query(
    sessions, monkeypatch, action
):
    first = DAY - timedelta(days=28)
    # Equal duplicate observations contribute one daily value and two evidence
    # references. Unselected methods/sources/metrics must consume neither budget.
    records = [
        summary(KIND, f"{i}-{j}", day=first + timedelta(days=i), averagePercentage=90 + i)
        for i in range(31)
        for j in range(2)
    ]
    await store(sessions, KIND, *records)
    await resting(sessions, [50, 60, 70], source=PHONE)
    args = dict(end_date=str(DAY + timedelta(days=2)))
    whole = await call(action, **args)
    chosen = next(r for r in whole["summaries"] if r["metric"] == "spo2")
    selection = {key: chosen[key] for key in ("metric", "source", "definition")}
    expected = [r for r in whole["summaries"] if r["metric"] == "spo2"]
    evidence_count = sum(len(r["evidence"]) for r in expected)
    assert evidence_count == 2 * (29 + 30 + 31) == 180
    async with sessions() as session:
        before = await snapshot(session)
    for budget in (evidence_count + 1, evidence_count):
        monkeypatch.setattr(indexing, "MAX_BASELINE_EVIDENCE", budget)
        page = await call(action, limit=1, **args, **selection)
        assert page["summaries"] == expected[:1] and page["next_cursor"]
        rest = await call(action, cursor=page["next_cursor"], limit=200, **args, **selection)
        assert page["summaries"] + rest["summaries"] == expected
    monkeypatch.setattr(indexing, "MAX_BASELINE_EVIDENCE", evidence_count - 1)
    with monkeypatch.context() as patch:
        blocked = block_generation(patch)
        for limit in (1, 200):
            for cursor in (None, page["next_cursor"]):
                assert_refusal(
                    await call(action, limit=limit, cursor=cursor, **args, **selection),
                    "baseline_evidence_budget_exceeded",
                )
        # The unfiltered query also exceeds the ceiling.
        assert_refusal(await call(action, **args), "baseline_evidence_budget_exceeded")
        assert (await call(action, source="absent", **args))["summaries"] == []
        blocked.assert_not_called()
    async with sessions() as session:
        assert await snapshot(session) == before


async def test_default_evidence_budget_rejects_small_input_with_large_repeated_history(
    sessions, monkeypatch
):
    records = [
        summary(KIND, f"{i}-{j}", day=DAY - timedelta(days=i), averagePercentage=95)
        for i in range(180)
        for j in range(8)
    ]
    assert len(records) < MAX_INPUT_RECORDS
    assert sum(len(canonical_json(r).encode()) for r in records) < MAX_INPUT_BYTES
    assert 90 * 91 * 8 > indexing.MAX_BASELINE_EVIDENCE
    await store(sessions, KIND, *records)
    with monkeypatch.context() as patch:
        blocked = block_generation(patch)
        for limit in (1, 200):
            assert_refusal(
                await call(
                    "analyze",
                    start_date=str(DAY - timedelta(days=89)),
                    lookback_days=90,
                    limit=limit,
                ),
                "baseline_evidence_budget_exceeded",
            )
        blocked.assert_not_called()


@pytest.mark.parametrize(
    "offset,breaks", [(-15, False), (-14, True), (-1, True), (0, False), (1, False)]
)
@pytest.mark.parametrize("other", ["method", "source", "metric"])
async def test_method_index_keeps_half_open_history_and_unselected_missing_evidence(
    sessions, offset, breaks, other
):
    await store(
        sessions, KIND, summary(KIND, "current", averagePercentage=95, measurementMethod="A")
    )
    row = (await call("baseline"))["summaries"][0]
    if other == "metric":
        await resting(sessions, [None], start=DAY + timedelta(days=offset))
    else:
        await store(
            sessions,
            KIND,
            summary(
                KIND,
                "other",
                day=DAY + timedelta(days=offset),
                averagePercentage=None,
                source=PHONE if other == "source" else BAND,
                measurementMethod="B",
            ),
        )
    selection = {key: row[key] for key in ("metric", "source", "definition")}
    selected = (await call("baseline", lookback_days=14, **selection))["summaries"][0]
    assert ("definition_or_method_break" in selected["reason_codes"]) == (
        breaks and other == "method"
    )
    assert selected["statistics"]["count"] == 0 and len(selected["evidence"]) == 1
    whole = (await call("baseline", lookback_days=14))["summaries"]
    assert selected == next(r for r in whole if all(r[k] == v for k, v in selection.items()))


async def test_indexed_windows_keep_robust_statistics_evidence_and_absolute_candidates(
    sessions, monkeypatch
):
    first = DAY - timedelta(days=28)
    await resting(sessions, list(range(10, 38)), start=first)
    await resting(sessions, [0, 80])
    await resting(sessions, [50] * 28, start=first, source=PHONE)
    await resting(sessions, [90, 0], source=PHONE)
    await store(sessions, KIND, summary(KIND, averagePercentage=95))
    args = dict(end_date=str(DAY + timedelta(days=1)))
    captured = {}
    original = health_interpretation.daily_series

    def capture(*args):
        captured.clear()
        captured.update(original(*args))
        return captured

    monkeypatch.setattr(health_interpretation, "daily_series", capture)
    result = await call("analyze", **args)
    Interpretation.model_validate(result)
    for row in result["summaries"]:
        key = next(
            k
            for k in captured
            if all(
                k[i] == row[field]
                for i, field in enumerate(
                    ("metric", "source", "definition", "unit", "method_version", "time_basis")
                )
            )
        )
        day = next(d for d in captured[key] if str(d) == row["date"])
        prior = [
            (d, r)
            for d, r in sorted(captured[key].items())
            if day - timedelta(days=56) <= d < day and r["value"] is not None and r["comparable"]
        ]
        assert row["comparison_dates"] == [str(d) for d, _ in prior]
        assert (
            row["evidence"]
            == [e for _, r in prior for e in r["evidence"]] + captured[key][day]["evidence"]
        )
        values = [r["value"] for _, r in prior]
        assert row["support_days"] == len(values)
        if values:
            center, spread = median(values), median(abs(v - median(values)) for v in values)
            assert row["statistics"] == dict(
                count=len(values),
                median=center,
                mad=spread,
                minimum=min(values),
                maximum=max(values),
                p10=quantiles(values, n=10, method="inclusive")[0],
                p90=quantiles(values, n=10, method="inclusive")[-1],
            )
            assert row["percentile"] == 100 * sum(v < row["value"] for v in values) / len(values)
            assert row["robust_z"] == (
                pytest.approx((row["value"] - center) / (1.4826 * spread)) if spread else None
            )
    assert len(result["candidates"]) == 4
    for candidate in result["candidates"]:
        row = result["summaries"][candidate["summary_index"]]
        assert candidate["evidence_dates"] == [row["date"], *row["comparison_dates"]]
    page = await call("analyze", limit=1, **args)
    rest = await call("analyze", limit=200, cursor=page["next_cursor"], **args)
    assert page["summaries"] + rest["summaries"] == result["summaries"]
    assert page["candidates"] == rest["candidates"] == result["candidates"]
    # An unselected method still changes the snapshot and the historical break.
    selection = dict(metric="resting_bpm", source="kaynak-" + payload_hash(BAND))
    page = await call("analyze", limit=1, **args, **selection)
    await resting(sessions, [50], start=DAY - timedelta(days=1), method="different")
    assert_refusal(
        await call("analyze", cursor=page["next_cursor"], **args, **selection), "stale_cursor"
    )
    assert (
        "definition_or_method_break"
        in (await call("analyze", **args, **selection))["summaries"][0]["reason_codes"]
    )


async def daily_metrics(sessions, first, days, sleep_too=False):
    """One record a day for each daily metric, as one ordinary band produces them."""
    span = [first + timedelta(days=i) for i in range(days)]
    kinds = {
        "daily-resting-heart-rate": dict(beatsPerMinute=60),
        "daily-heart-rate-variability": dict(averageHeartRateVariabilityMilliseconds=40),
        KIND: dict(averagePercentage=96),
    }
    for kind, fields in kinds.items():
        await store(sessions, kind, *[summary(kind, str(day), day=day, **fields) for day in span])
    moments = [T0 + timedelta(days=(day - DAY).days) for day in span]
    whole_day = [steps(str(at), start=at, end=at + timedelta(days=1)) for at in moments]
    await store(sessions, "steps", *whole_day)
    if sleep_too:
        asleep = {"minutesAsleep": 420}
        await store(
            sessions,
            "sleep",
            *[
                sleep(str(at), at + timedelta(hours=1), at + timedelta(hours=8), summary=asleep)
                for at in moments
            ],
        )


@pytest.mark.parametrize("lookback,metrics", [(56, 4), (90, 5)])
async def test_default_evidence_budget_allows_full_range_of_daily_metrics(
    sessions, lookback, metrics
):
    first = DAY - timedelta(days=89)
    await daily_metrics(sessions, first - timedelta(days=lookback), 90 + lookback, metrics == 5)
    # Every summary repeats its full history: metrics * 90 * (lookback + 1) references.
    result = await call("baseline", start_date=str(first), lookback_days=lookback, limit=1)
    assert "code" not in result, result["code"]
    assert result["total_summaries"] == metrics * 90
    assert len(result["summaries"][0]["evidence"]) == lookback + 1
