"""Adapters through SQLite and the actual health_archive production tool."""

import json
from copy import deepcopy
from datetime import date, timedelta
from itertools import permutations

import derived_records as archive
import pytest
from derived_records import (
    BAND,
    DAY,
    PHONE,
    T0,
    cache,
    point,
    read,
    sleep,
    snapshot,
    steps,
    streams,
    summary,
)
from numeric_records import store_read_correction as store
from sleep_numeric_records import BAD_SLEEP_NUMBERS, evidence
from sqlalchemy import select

from app.models.derived import DerivedDay
from app.models.state import RawHealthData
from app.services import codex_health
from app.services.canonical_daily import METHOD, bounds, canonical_day
from app.services.canonical_types import CanonicalDay
from app.services.codex_health import read_health


async def test_five_adapters_reach_production_tool_without_changing_legacy_or_coverage(
    sessions, monkeypatch
):
    await store(sessions, "steps", steps(count=0))
    await store(sessions, "sleep", sleep(summary={"minutesAsleep": 0}))
    await store(
        sessions, "daily-resting-heart-rate", summary("daily-resting-heart-rate", beatsPerMinute=0)
    )
    await store(
        sessions,
        "daily-heart-rate-variability",
        summary(
            "daily-heart-rate-variability",
            averageHeartRateVariabilityMilliseconds=11,
            rootMeanSquareOfSuccessiveDifferencesMilliseconds=22,
            deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds=33,
        ),
    )
    await store(
        sessions, "daily-oxygen-saturation", summary("daily-oxygen-saturation", averagePercentage=0)
    )
    monkeypatch.setattr(codex_health, "SessionLocal", sessions)
    args = {"action": "daily", "start_date": str(DAY), "end_date": str(DAY), "timezone": "UTC"}
    before_coverage = await read_health(dict(args, action="coverage"))
    async with sessions() as session:
        before = await snapshot(session)
    result = await read_health(args)
    assert result["days"][0]["steps"] == 0 and result["profile"] == {}
    assert "charts" in result and "sleep_method" in result
    card = result["derived"]
    assert card["schema_version"] == "1" and card["source_selection"] == "none"
    values = streams(card)
    assert len(values) == 7
    assert {row["metric"] for row in values} == {
        "steps",
        "sleep_seconds",
        "resting_bpm",
        "hrv_ms",
        "spo2",
    }
    assert {row["value"] for row in streams(card, "hrv_ms")} == {11, 22, 33}
    assert {row["definition"] for row in streams(card, "hrv_ms")} == {
        "daily_average_hrv",
        "daily_rmssd",
        "deep_sleep_rmssd",
    }
    assert all(row["value"] == 0 for row in values if row["metric"] != "hrv_ms")
    assert all(row["method_version"] == METHOD for row in values)
    assert all(row["source"].startswith("kaynak-") and row["observations"] for row in values)
    assert {row["unit"] for row in values} == {"count", "s", "bpm", "ms", "%"}
    again = await read_health(args)
    assert again["derived"] == card and (await cache(sessions)).build_count == 1
    after_coverage = await read_health(dict(args, action="coverage"))
    before_coverage.pop("computed_at")
    after_coverage.pop("computed_at")
    assert after_coverage == before_coverage
    assert all(row["retrieval"]["state"] == "unknown" for row in after_coverage["metrics"])
    async with sessions() as session:
        assert await snapshot(session) == before
    # Original Google payloads still travel through records; no narrowed model scope.
    raw = await read_health(dict(args, action="records", data_type="daily-heart-rate-variability"))
    assert (
        raw["records"][0]["payload"]["dailyHeartRateVariability"][
            "averageHeartRateVariabilityMilliseconds"
        ]
        == 11
    )


@pytest.mark.parametrize("sources", [(PHONE, BAND), (None, None), ({}, {})])
async def test_overlapping_phone_and_band_are_separate_and_not_ranked(sessions, sources):
    await store(
        sessions,
        "steps",
        steps("phone", 300, source=sources[0]),
        steps("band", 100, source=sources[1]),
    )
    card = await read(sessions)
    assert sorted(row["value"] for row in streams(card, "steps")) == [100, 300]
    assert len({row["source"] for row in streams(card)}) == 2
    assert all("overlapping_intervals" not in row["uncertainties"] for row in streams(card))
    assert card["days"][0]["result"]["source_selection"] == "none"


async def test_same_stream_summary_conflicts_and_alias_conflicts_are_explicit(sessions):
    await store(
        sessions,
        "daily-heart-rate-variability",
        summary(
            "daily-heart-rate-variability",
            "a",
            rmssd=10,
            rootMeanSquareOfSuccessiveDifferencesMilliseconds=20,
        ),
        summary("daily-heart-rate-variability", "b", rmssd=30),
    )
    result = streams(await read(sessions), "hrv_ms")
    assert len(result) == 1 and result[0]["value"] is None
    assert "conflicting_values" in result[0]["uncertainties"]
    assert {item["value"] for item in result[0]["observations"]} == {10, 20, 30}


async def test_same_stream_step_overlap_is_not_summed_and_disjoint_values_are(sessions):
    await store(sessions, "steps", steps("a"), steps("b", 30, start=T0 + timedelta(minutes=30)))
    value = streams(await read(sessions), "steps")[0]
    assert value["value"] is None and "overlapping_intervals" in value["uncertainties"]
    await store(sessions, "steps", steps("b", 30, start=T0 + timedelta(hours=1)))
    value = streams(await read(sessions), "steps")[0]
    assert value["value"] == 150 and "overlapping_intervals" not in value["uncertainties"]


@pytest.mark.parametrize("missing", [None, "invalid", True])
async def test_missing_value_never_becomes_zero(sessions, missing):
    await store(
        sessions,
        "daily-oxygen-saturation",
        summary("daily-oxygen-saturation", averagePercentage=missing),
    )
    result = streams(await read(sessions), "spo2")[0]
    assert result["value"] is None and "no_value" in result["uncertainties"]


async def test_intraday_hrv_and_spo2_are_not_daily_summaries(sessions):
    for kind, field in (("heart-rate-variability", "rmssd"), ("oxygen-saturation", "percentage")):
        await store(
            sessions,
            kind,
            point(
                kind,
                "sample",
                {
                    "sampleTime": {"physicalTime": T0.isoformat()},
                    field: 999,
                },
            ),
        )
    assert streams(await read(sessions)) == []
    await store(
        sessions, "daily-heart-rate-variability", summary("daily-heart-rate-variability", rmssd=12)
    )
    assert streams(await read(sessions), "hrv_ms")[0]["value"] == 12


@pytest.mark.parametrize("timezone", ["Pacific/Honolulu", "Pacific/Kiritimati", "Europe/Paris"])
async def test_summary_civil_date_is_not_shifted_to_query_timezone(sessions, timezone):
    await store(
        sessions, "daily-resting-heart-rate", summary("daily-resting-heart-rate", beatsPerMinute=50)
    )
    card = await read(sessions, DAY - timedelta(days=1), DAY + timedelta(days=1), timezone)
    assert [len(streams(card, "resting_bpm", i)) for i in range(3)] == [0, 1, 0]
    assert streams(card, "resting_bpm", 1)[0]["observations"][0]["source_day"] == str(DAY)


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
async def test_step_allocation_uses_real_23_and_25_hour_civil_days(sessions, day, hours):
    left, right = bounds(day, "Europe/Paris")
    # One hour either side belongs to adjacent days; UTC arithmetic avoids DST loss.
    await store(
        sessions,
        "steps",
        steps(
            start=left - timedelta(hours=1), end=right + timedelta(hours=1), count=(hours + 2) * 60
        ),
    )
    card = await read(sessions, day, timezone="Europe/Paris")
    assert card["days"][0]["result"]["day_seconds"] == hours * 3600
    assert streams(card, "steps")[0]["value"] == hours * 60


async def test_arrival_order_and_last_seen_never_choose_a_canonical_value(sessions):
    records = [summary("daily-resting-heart-rate", str(i), beatsPerMinute=50 + i) for i in range(3)]
    for record in records:
        await store(sessions, "daily-resting-heart-rate", record)
    async with sessions() as session:
        expected = await canonical_day(session, DAY, "UTC")
    for order in permutations(records):
        for record in order:
            await store(sessions, "daily-resting-heart-rate", deepcopy(record))
        async with sessions() as session:
            assert await canonical_day(session, DAY, "UTC") == expected
    assert expected["streams"][0]["value"] is None


async def test_resting_calculation_definitions_stay_separate(sessions):
    await store(
        sessions,
        "daily-resting-heart-rate",
        summary(
            "daily-resting-heart-rate",
            "a",
            beatsPerMinute=50,
            dailyRestingHeartRateMetadata={"calculationMethod": "A"},
        ),
        summary(
            "daily-resting-heart-rate",
            "b",
            beatsPerMinute=60,
            dailyRestingHeartRateMetadata={"calculationMethod": "B"},
        ),
    )
    values = streams(await read(sessions), "resting_bpm")
    assert sorted(row["value"] for row in values) == [50, 60]
    assert len({row["definition"] for row in values}) == 2


async def test_initial_fetch_orders_produce_identical_results(sessions):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.base import Base

    records = [
        steps("a", 10),
        steps("b", 20, start=T0 + timedelta(hours=1)),
        steps("phone", 30, source=PHONE),
    ]
    await store(sessions, "steps", *records)
    expected = streams(await read(sessions))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        reversed_sessions = async_sessionmaker(engine, expire_on_commit=False)
        for record in reversed(records):
            await store(reversed_sessions, "steps", record)
        assert streams(await read(reversed_sessions)) == expected
    finally:
        await engine.dispose()


@pytest.mark.parametrize("offset", [0, 1])
@pytest.mark.parametrize("missing", [None, "invalid", True])
async def test_missing_step_correction_does_not_reuse_previous_normalized_count(
    sessions, offset, missing
):
    await store(sessions, "steps", steps(count=120))
    assert streams(await read(sessions), "steps")[0]["value"] == 120
    await store(sessions, "steps", steps(count=missing, start=T0 + timedelta(days=offset)))
    card = await read(sessions, DAY, DAY + timedelta(days=offset))
    if offset:
        assert streams(card, "steps", 0) == []
    result = streams(card, "steps", offset)[0]
    assert result["value"] is None and result["state"] == "no_value"
    await store(sessions, "steps", steps(count=0))
    assert streams(await read(sessions), "steps")[0]["value"] == 0


@pytest.mark.parametrize("with_valid_interval", [False, True])
async def test_missing_step_count_prevents_partial_daily_totals(sessions, with_valid_interval):
    await store(sessions, "steps", steps("gap", count=None))
    if with_valid_interval:
        await store(sessions, "steps", steps("valid", count=20, start=T0 + timedelta(hours=1)))
    result = streams(await read(sessions), "steps")[0]
    assert result["value"] is None and "missing_observations" in result["uncertainties"]


@pytest.mark.parametrize("kind", ["steps", "sleep"])
async def test_undated_correction_does_not_reuse_previous_normalized_interval(sessions, kind):
    record = steps() if kind == "steps" else sleep()
    await store(sessions, kind, record)
    assert streams(await read(sessions))
    record[kind].pop("interval")
    await store(sessions, kind, record)
    assert streams(await read(sessions)) == []


@pytest.mark.parametrize(
    "supplied", ["1e10000", "-1e10000", "NaN", "Infinity", "-Infinity", "1e-10000"]
)
async def test_unrepresentable_summary_keeps_zero_and_sound_streams(sessions, supplied):
    kind = "daily-heart-rate-variability"
    await store(sessions, kind, summary(kind, "bad", rmssd=supplied))
    async with sessions() as session:
        before = await snapshot(session)
    card = await read(sessions)
    row = streams(card, "hrv_ms")[0]
    assert row["state"] == "no_value" and row["value"] is None
    assert row["observations"][0]["value"] is None
    assert {"no_value", "missing_observations"} <= set(row["uncertainties"])
    async with sessions() as session:
        assert await snapshot(session) == before
    await store(
        sessions,
        kind,
        summary(kind, "zero", rmssd=0),
        summary(kind, "sound", source=PHONE, rmssd=12),
    )
    async with sessions() as session:
        # Reads may write only the disposable cache, including after a correction.
        after_ingest = await snapshot(session)
    card = await read(sessions)
    assert sorted(row["value"] for row in streams(card, "hrv_ms")) == [0, 12]
    damaged = next(row for row in streams(card) if len(row["observations"]) == 2)
    assert {obs["value"] for obs in damaged["observations"]} == {None, 0}
    assert "missing_observations" in damaged["uncertainties"]
    CanonicalDay.model_validate(card["days"][0]["result"])
    json.dumps(card, allow_nan=False)
    async with sessions() as session:
        assert await snapshot(session) == after_ingest


@pytest.mark.parametrize("sign", [1, -1])
async def test_step_intermediates_and_unrepresentable_total_are_distinguished(sessions, sign):
    value = sign * 1e308
    await archive.store(sessions, "steps", steps("a", count=value))
    card = await read(sessions)
    assert streams(card, "steps")[0]["value"] == value
    await archive.store(
        sessions,
        "steps",
        steps("b", count=value, start=T0 + timedelta(hours=1)),
        steps("sound", count=0, source=PHONE),
    )
    async with sessions() as session:
        before = await snapshot(session)
    card = await read(sessions)
    damaged = next(row for row in streams(card) if len(row["observations"]) == 2)
    assert damaged["value"] is None and damaged["state"] == "no_value"
    assert "numeric_overflow:daily_total" in damaged["uncertainties"]
    assert [obs["value"] for obs in damaged["observations"]] == [value, value]
    assert any(row["value"] == 0 for row in streams(card))
    CanonicalDay.model_validate(card["days"][0]["result"])
    json.dumps(card, allow_nan=False)
    async with sessions() as session:
        assert await snapshot(session) == before
    await archive.store(sessions, "steps", steps("c", count=-value, start=T0 + timedelta(hours=2)))
    assert value in {row["value"] for row in streams(await read(sessions))}


@pytest.mark.parametrize("supplied", ["1e10000", "-1e10000", "NaN", "Infinity", "1e308"])
async def test_sleep_unit_overflow_is_not_clamped_to_a_full_session(
    sessions, monkeypatch, supplied
):
    await store(
        sessions,
        "sleep",
        sleep("bad", summary={"minutesAsleep": supplied}),
        sleep(
            "zero",
            summary={"minutesAsleep": 0},
            start=T0 + timedelta(hours=4),
            end=T0 + timedelta(hours=5),
        ),
    )
    async with sessions() as session:
        before = await snapshot(session)
    card = await read(sessions)
    row = streams(card, "sleep_seconds")[0]
    assert row["value"] is None and row["state"] == "no_value"
    assert [e["value"] for e in row["observations"]] == [None, 0]
    assert "numeric_overflow:sleep_estimate" in row["uncertainties"]
    assert "unsupported_or_missing_value:asleep_seconds" in row["sleep"]["reason_codes"]
    assert row["sleep"]["sessions"][0]["supplied_asleep_seconds"] is None
    monkeypatch.setattr(codex_health, "SessionLocal", sessions)
    result = await read_health(
        {"action": "sleep", "start_date": str(DAY), "end_date": str(DAY), "timezone": "UTC"}
    )
    assert result["summaries"][0]["value"] is None
    json.dumps(result, allow_nan=False)
    CanonicalDay.model_validate(card["days"][0]["result"])
    async with sessions() as session:
        assert await snapshot(session) == before


async def test_sleep_finite_period_and_awake_values_can_overflow_estimate(sessions):
    await store(
        sessions,
        "sleep",
        sleep(
            duration="1e308s",
            stages=[
                {"type": "AWAKE", "duration": "-1e308s"},
            ],
        ),
    )
    card = await read(sessions)
    row = streams(card, "sleep_seconds")[0]
    assert row["value"] is None and row["observations"][0]["value"] is None
    assert "numeric_overflow:sleep_estimate" in row["uncertainties"]
    assert row["sleep"]["sessions"][0]["stage_totals_seconds"]["awake"] == -1e308
    CanonicalDay.model_validate(card["days"][0]["result"])
    json.dumps(card, allow_nan=False)


async def test_sleep_stage_total_overflow_keeps_independent_sound_asleep_summaries(sessions):
    await store(
        sessions,
        "sleep",
        *[
            sleep(
                str(index),
                start=T0 + timedelta(hours=left),
                end=T0 + timedelta(hours=left + 1),
                summary={"minutesAsleep": 0},
                stages=[
                    {
                        "type": "AWAKE",
                        "duration": "1e308s",
                        "startTime": (T0 + timedelta(hours=left)).isoformat(),
                        "endTime": (T0 + timedelta(hours=left + 1)).isoformat(),
                    }
                ],
            )
            for index, left in enumerate((1, 4))
        ],
    )
    card = await read(sessions)
    row = streams(card, "sleep_seconds")[0]
    assert row["value"] == 0 and [e["value"] for e in row["observations"]] == [0, 0]
    assert row["sleep"]["stage_total_differences_seconds"]["awake"] is None
    assert "numeric_overflow:stage_total_difference:awake" in row["sleep"]["reason_codes"]
    CanonicalDay.model_validate(card["days"][0]["result"])
    json.dumps(card, allow_nan=False)


@pytest.mark.parametrize("supplied", BAD_SLEEP_NUMBERS)
@pytest.mark.parametrize("field", ["stage_awake", "summary_awake", "stage_light"])
async def test_sound_classic_summary_does_not_use_bad_independent_stage_totals(
    sessions, field, supplied
):
    payload = evidence(field, supplied)
    payload.setdefault("summary", {})["minutesAsleep"] = 0
    await store(sessions, "sleep", sleep(**payload))
    async with sessions() as session:
        before = await snapshot(session)
    card = await read(sessions)
    row = streams(card, "sleep_seconds")[0]
    assert row["value"] == row["observations"][0]["value"] == 0
    assert "numeric_overflow:sleep_estimate" not in row["uncertainties"]
    totals = row["sleep"]["sessions"][0]["stage_totals_seconds"]
    assert list(totals.values()) == [None]
    assert any(
        code.startswith("unsupported_or_missing_value:stage_total:")
        for code in row["sleep"]["reason_codes"]
    )
    CanonicalDay.model_validate(card["days"][0]["result"])
    json.dumps(card, allow_nan=False)
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize("previous,daily", [("source-daily/4", False), ("source-daily/5", True)])
async def test_previous_method_cache_is_preserved_and_rebuilt(sessions, previous, daily):
    await store(
        sessions,
        "sleep",
        sleep(
            duration="1e308s",
            stages=[
                {"type": "AWAKE", "duration": "-1e308s"},
            ],
        ),
    )
    kind = "daily-resting-heart-rate"
    if daily:
        await store(sessions, kind, summary(kind, beatsPerMinute=60))
    card = await read(sessions)
    legacy = deepcopy(card["days"][0]["result"])
    legacy["method_version"] = previous
    legacy["streams"][0].update(
        method_version=previous, value=7200, state="estimated", uncertainties=[]
    )
    legacy["streams"][0]["observations"][0].update(value=7200, assumptions=[])
    async with sessions() as session, session.begin():
        row = await session.get(DerivedDay, (DAY, "UTC", METHOD))
        row.method, row.result = previous, legacy
        if daily:
            # Keep the old parser's timestamp and normalized value for this raw date.
            raw = await session.scalar(select(RawHealthData).where(RawHealthData.data_type == kind))
            bad = dict(year=2026.9, month=9.9, day=29.9)
            raw.raw_json = deepcopy(raw.raw_json)
            raw.raw_json["dailyRestingHeartRate"]["date"] = bad
        await session.flush()
        await session.refresh(row)
        row.built_revision = row.revision
        before = await snapshot(session)
    card = await read(sessions)
    assert card["method_version"] == METHOD
    assert streams(card, "sleep_seconds")[0]["value"] is None
    assert streams(card, "resting_bpm") == []
    async with sessions() as session:
        old = await session.get(DerivedDay, (DAY, "UTC", previous))
        assert old.result == legacy
        assert await session.get(DerivedDay, (DAY, "UTC", METHOD)) is not None
        assert await snapshot(session) == before


def test_reference_names_the_current_daily_method():
    from pathlib import Path

    from app.services.canonical_daily import METHOD

    reference = (Path(__file__).parents[1] / "docs" / "REFERENCE.md").read_text(encoding="utf-8")
    assert f"method `{METHOD}`" in reference
