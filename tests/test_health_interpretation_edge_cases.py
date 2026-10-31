"""Interpretation edge cases through the real reader and SQLite archive."""

import json
from datetime import date, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from derived_records import (
    DAY,
    PHONE,
    T0,
    manual,
    point,
    snapshot,
    steps,
    store,
    summary,
)
from interpretation_records import call, resting
from interpretation_records import production_reader as production_reader
from sqlalchemy import select

from app.models.derived import DerivedDay
from app.services import codex_health, health_interpretation, interpretation_inputs
from app.services.interpretation_types import ACTIONS, Interpretation, Refusal
from app.services.manual_flags import record_flag


@pytest.mark.parametrize("action", sorted(ACTIONS))
@pytest.mark.parametrize("lookback", [14, 56, 90])
@pytest.mark.parametrize("zone,extra", [("UTC", -90), ("UTC", -1), ("UTC", 0), ("Etc/GMT-14", 1)])
async def test_unrepresentable_interpretation_window_refuses_before_any_database_access(
    monkeypatch, action, lookback, zone, extra
):
    history = lookback if action in {"baseline", "analyze"} else 0
    day = date.min + timedelta(days=max(0, history + extra))
    sessions = Mock(side_effect=AssertionError("must validate before opening a session"))
    build = AsyncMock(side_effect=AssertionError("must validate before building derived days"))
    monkeypatch.setattr(codex_health, "SessionLocal", sessions)
    monkeypatch.setattr(codex_health, "build_range", build)
    result = await call(
        action,
        start_date=str(day),
        end_date=str(day),
        timezone=zone,
        lookback_days=lookback,
        metric="steps",
        other_metric="resting_bpm",
    )
    assert Refusal.model_validate(result).code == "invalid_query"
    assert result["reason_codes"] == ["invalid_query"]
    sessions.assert_not_called()
    build.assert_not_called()


@pytest.mark.parametrize("action", ["baseline", "analyze"])
@pytest.mark.parametrize("lookback", [14, 56, 90])
@pytest.mark.parametrize("zone,margin", [("UTC", 1), ("Etc/GMT-14", 2), ("Etc/GMT+12", 1)])
async def test_first_representable_baseline_keeps_entire_history_and_separate_zero_sources(
    sessions, action, lookback, zone, margin
):
    first = date.min + timedelta(days=margin)
    day = first + timedelta(days=lookback)
    await resting(sessions, [50] * lookback + [0], start=first)
    await resting(sessions, [0], start=day, source=PHONE)
    async with sessions() as session:
        before = await snapshot(session)
    result = await call(
        action,
        start_date=str(day),
        end_date=str(day),
        timezone=zone,
        lookback_days=lookback,
        metric="resting_bpm",
    )
    rows = Interpretation.model_validate(result).summaries
    assert len(rows) == 2 and len({r.source for r in rows}) == 2
    reference = next(r for r in result["summaries"] if r["eligible"])
    assert all(row["value"] == 0 for row in result["summaries"])
    assert reference["statistics"]["count"] == lookback
    expected = [str(first + timedelta(days=i)) for i in range(lookback)]
    assert reference["comparison_dates"] == expected
    async with sessions() as session:
        assert await snapshot(session) == before
        cached = list(await session.scalars(select(DerivedDay).order_by(DerivedDay.day)))
        assert [r.day for r in cached] == [first + timedelta(days=i) for i in range(lookback + 1)]
        assert all(r.revision == r.built_revision for r in cached)


@pytest.mark.parametrize("action", sorted(ACTIONS - {"baseline", "analyze"}))
@pytest.mark.parametrize("zone,margin", [("UTC", 1), ("Etc/GMT-14", 2), ("Etc/GMT+12", 1)])
async def test_other_interpretations_accept_first_representable_padded_day(
    sessions, action, zone, margin
):
    day = date.min + timedelta(days=margin)
    await resting(sessions, [0], start=day)
    async with sessions() as session:
        before = await snapshot(session)
    result = await call(
        action,
        start_date=str(day),
        end_date=str(day),
        timezone=zone,
        lookback_days=90,
        metric="resting_bpm",
        other_metric="resting_bpm",
        lags=[-14, 0, 14],
    )
    assert Interpretation.model_validate(result).action == action
    if action == "relationship":
        assert [r["support_pairs"] for r in result["summaries"]] == [0, 1, 0]
    async with sessions() as session:
        assert await snapshot(session) == before


async def test_unrelated_intraday_records_do_not_consume_daily_input_budget(sessions, monkeypatch):
    await resting(sessions, [50])
    await store(
        sessions,
        "heart-rate",
        *[
            point(
                "heart-rate",
                str(i),
                {
                    "sampleTime": {"physicalTime": (T0 + timedelta(seconds=i)).isoformat()},
                    "beatsPerMinute": 60,
                },
            )
            for i in range(10)
        ],
    )
    monkeypatch.setattr(interpretation_inputs, "MAX_INPUT_RECORDS", 5)
    assert "error" not in await call("baseline", metric="resting_bpm")
    assert (await call("intraday", data_type="heart-rate"))["code"] == "input_budget_exceeded"


async def test_change_during_calculation_is_refused_instead_of_mixing_snapshots(
    sessions, monkeypatch
):
    def weight(value):
        return point(
            "weight", "one", {"sampleTime": {"physicalTime": T0.isoformat()}, "weightGrams": value}
        )

    await store(sessions, "weight", weight(70000))
    original = health_interpretation.load_inputs
    reads = 0

    async def changing(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 2:
            await store(sessions, "weight", weight(80000))
        return await original(*args, **kwargs)

    monkeypatch.setattr(health_interpretation, "load_inputs", changing)
    assert (await call("longitudinal", data_type="weight"))["code"] == "inputs_changed_retry"


@pytest.mark.parametrize("source", [None, "opaque", []])
async def test_unknown_intraday_sources_are_not_implicitly_one_device(sessions, source):
    await store(
        sessions,
        "heart-rate",
        *[
            point(
                "heart-rate",
                str(i),
                {
                    "sampleTime": {"physicalTime": (T0 + timedelta(seconds=60 * i)).isoformat()},
                    "beatsPerMinute": 60 + i,
                },
                source=source,
            )
            for i in range(2)
        ],
    )
    rows = (await call("intraday", data_type="heart-rate"))["summaries"]
    assert len(rows) == 2 and all(row["supported_seconds"] == 0 for row in rows)


@pytest.mark.parametrize(
    "supplied,reason",
    [
        *[(v, "numeric_overflow:unit_conversion") for v in ("1e10000", "-1e10000")],
        *[(v, "unsupported_or_missing_value") for v in ("NaN", "Infinity", "-Infinity")],
    ],
)
async def test_numeric_strings_are_missing_evidence_alongside_measured_zero(
    sessions, supplied, reason
):
    kind = "daily-sleep-temperature-derivations"
    await store(sessions, kind, summary(kind, "bad", baselineTemperatureCelsius=36))
    await store(sessions, kind, summary(kind, "bad", baselineTemperatureCelsius=supplied))
    await store(
        sessions,
        kind,
        summary(kind, "zero", day=DAY - timedelta(days=1), baselineTemperatureCelsius=0),
    )
    await manual(sessions, 70)
    flagged = await manual(sessions, 999)
    async with sessions() as session, session.begin():
        await record_flag(session, flagged, "test", True)
        before = await snapshot(session)
    result = await call("physiology", data_type=kind, start_date=str(DAY - timedelta(days=1)))
    row = result["summaries"][0]
    assert row["state"] == "available" and row["statistics"]["count"] == 1
    assert row["statistics"]["median"] == 0
    assert reason in row["reason_codes"]
    assert [e["value"] for e in row["evidence"]] == [0, None]
    Interpretation.model_validate(result)
    json.dumps(result, allow_nan=False)
    await call("longitudinal", data_type="weight")
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize(
    "values,center,mad,p10,p90",
    [
        ([-1.7e308, 1.7e308], 0, 1.7e308, -1.36e308, 1.36e308),
        ([1.7e308, 1.7e308], 1.7e308, 0, 1.7e308, 1.7e308),
        ([-1.7e308, -1.7e308, 1.7e308], -1.7e308, 0, -1.7e308, 1.02e308),
    ],
)
async def test_finite_extremes_keep_representable_statistics_and_explain_change(
    sessions, values, center, mad, p10, p90
):
    for index, value in enumerate(values):
        await store(
            sessions,
            "body-fat",
            point(
                "body-fat",
                str(index),
                {
                    "sampleTime": {"physicalTime": (T0 + timedelta(minutes=index)).isoformat()},
                    "percentage": value,
                },
            ),
        )
    async with sessions() as session:
        before = await snapshot(session)
    result = await call("longitudinal", data_type="body-fat")
    row = result["summaries"][0]
    stats = row["statistics"]
    assert stats["median"] == center and stats["mad"] == mad
    assert stats["count"] == len(values)
    assert stats["p10"] == pytest.approx(p10) and stats["p90"] == pytest.approx(p90)
    assert [e["value"] for e in row["evidence"]] == values
    if values[0] != values[-1]:
        assert row["change"] is None and "numeric_overflow:change" in row["reason_codes"]
    else:
        assert row["change"] == 0
    assert result == await call("longitudinal", data_type="body-fat", limit=1)
    Interpretation.model_validate(result)
    json.dumps(result, allow_nan=False)
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize("scale,expected_z", [(1e-100, None), (1.7e308, 1 / 1.4826)])
async def test_robust_ratio_overflow_never_creates_a_candidate(sessions, scale, expected_z):
    await resting(sessions, [-scale, scale] * 14, start=DAY - timedelta(days=28))
    await resting(sessions, [1e308 if expected_z is None else scale])
    await store(
        sessions, "daily-oxygen-saturation", summary("daily-oxygen-saturation", averagePercentage=0)
    )
    async with sessions() as session:
        before = await snapshot(session)
    result = await call("analyze")
    row = next(r for r in result["summaries"] if r["metric"] == "resting_bpm")
    assert row["statistics"]["count"] == 28 and row["statistics"]["mad"] == scale
    if expected_z is None:
        assert row["robust_z"] is row["percentile"] is None and not row["eligible"]
        assert "numeric_overflow:robust_z" in row["reason_codes"]
    else:
        assert row["robust_z"] == pytest.approx(expected_z) and row["eligible"]
    assert not result["candidates"]
    assert any(r["metric"] == "spo2" and r["value"] == 0 for r in result["summaries"])
    Interpretation.model_validate(result)
    json.dumps(result, allow_nan=False)
    async with sessions() as session:
        assert await snapshot(session) == before


async def test_intraday_unrepresentable_rate_is_a_gap_with_sound_zero_retained(sessions):
    await store(
        sessions,
        "steps",
        steps("bad", count=1e308, end=T0 + timedelta(microseconds=1)),
        steps("zero", count=0, start=T0 + timedelta(hours=1)),
    )
    async with sessions() as session:
        before = await snapshot(session)
    result = await call("intraday", data_type="steps")
    row = result["summaries"][0]
    assert row["supported_seconds"] == 3600 and row["time_weighted_mean"] == 0
    assert row["gap_seconds"] == 86400 - 3600
    assert "numeric_overflow:interval_rate" in row["reason_codes"]
    assert row["cells"][0]["state"] == "gap" and row["cells"][0]["value"] is None
    # The count itself is representable even though its per-second rate is not.
    assert {e["value"] for e in row["evidence"]} == {1e308, 0}
    Interpretation.model_validate(result)
    json.dumps(result, allow_nan=False)
    async with sessions() as session:
        assert await snapshot(session) == before


async def test_unit_conversion_underflow_does_not_invent_a_measured_zero(sessions):
    for name, value, offset in [("tiny", "5e-324", 0), ("zero", 0, 1)]:
        await store(
            sessions,
            "distance",
            point(
                "distance",
                name,
                {
                    "interval": {
                        "startTime": (T0 + timedelta(hours=offset)).isoformat(),
                        "endTime": (T0 + timedelta(hours=offset + 1)).isoformat(),
                    },
                    "millimeters": value,
                },
            ),
        )
    async with sessions() as session:
        before = await snapshot(session)
    result = await call("longitudinal", data_type="distance")
    row = result["summaries"][0]
    assert {e["value"] for e in row["evidence"]} == {None, 0}
    assert row["statistics"]["count"] == 1 and row["statistics"]["median"] == 0
    assert "numeric_overflow:unit_conversion" in row["reason_codes"]
    Interpretation.model_validate(result)
    json.dumps(result, allow_nan=False)
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize("values,mean", [([1.7e308, 1.7e308], 1.7e308), ([-1.7e308, 1.7e308], 0)])
async def test_intraday_interpolation_and_weighted_mean_retry_intermediate_overflow(
    sessions, values, mean
):
    await store(
        sessions,
        "heart-rate",
        *[
            point(
                "heart-rate",
                str(i),
                {
                    "sampleTime": {"physicalTime": (T0 + timedelta(seconds=60 * i)).isoformat()},
                    "beatsPerMinute": value,
                },
            )
            for i, value in enumerate(values)
        ],
    )
    result = await call("intraday", data_type="heart-rate")
    row = result["summaries"][0]
    assert row["supported_seconds"] == 60 and row["time_weighted_mean"] == mean
    assert next(c for c in row["cells"] if c["state"] == "supported")["value"] == mean
    assert row["statistics"]["count"] == 2
    Interpretation.model_validate(result)
    json.dumps(result, allow_nan=False)
