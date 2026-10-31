"""The ``coverage`` action of ``health_archive`` (app/services/coverage.py), synthetic data."""

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from coverage_records import assert_sources
from coverage_records import source_archive as source_archive
from fastapi import WebSocketDisconnect
from pydantic import ValidationError
from test_codex_chat import RPC, Browser, take
from weight_records import INVALID_FIELDS

from app.api import codex
from app.models.health import HeartRateVariability, SleepSession, Steps, Weight
from app.models.identity import RawHealthDataSupersession
from app.models.manual import ManualMeasurement
from app.models.state import RawHealthData
from app.services import codex_chat, codex_health, coverage
from app.services.codex_health import HEALTH_TOOL, ArchiveQuery, read_health
from app.services.manual_flags import record_flag

BAND = {
    "platform": "FITBIT",
    "recordingMethod": "DERIVED",
    "device": {"displayName": "Example Band", "formFactor": "FITNESS_BAND"},
}
PHONE = {
    "platform": "HEALTH_KIT",
    "recordingMethod": "PASSIVELY_MEASURED",
    "device": {"formFactor": "PHONE", "manufacturer": "Example Inc."},
}
SEEN = datetime(2026, 10, 1, tzinfo=UTC)
JSON_KEYS = {
    "steps": "steps",
    "sleep": "sleep",
    "weight": "weight",
    "body-fat": "bodyFat",
    "daily-resting-heart-rate": "dailyRestingHeartRate",
    "daily-heart-rate-variability": "dailyHeartRateVariability",
    "daily-oxygen-saturation": "dailyOxygenSaturation",
}


@pytest.mark.parametrize("kind", ["steps", "daily-resting-heart-rate"])
async def test_source_labels_preserve_stored_measurements(archive, source_archive, kind):
    card = await read_health(query("2026-09-01", "2026-09-02", data_type=kind))
    metric = card["metrics"][0]
    assert (metric["value_days"], metric["days_without_value"]) == (1, 1)
    assert metric["sampling"]["sample_count"] == 3
    assert metric["retrieval"]["state"] == "unknown"
    assert_sources(metric["source_regimes"], source_archive)


def at(day: date, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def raw(kind, gid, start, payload, source=BAND, end=None, **overrides):
    values = {
        "id": uuid.uuid4(),
        "data_type": kind,
        "google_data_point_id": gid,
        "source_start_time": start,
        "source_end_time": end,
        "raw_json": {"dataSource": source, JSON_KEYS[kind]: payload},
        "payload_hash": "test",
        "first_seen_at": SEEN,
        "last_seen_at": SEEN,
    }
    return RawHealthData(**(values | overrides))


def steps(gid, start, minutes=10, count=12, source=BAND, raw_row=True, **flags):
    end = start + timedelta(minutes=minutes)
    norm_flags = {"is_quarantined": flags.pop("norm_quarantined", False)}
    rows = [
        Steps(google_data_point_id=gid, start_time=start, end_time=end, count=count, **norm_flags)
    ]
    if raw_row:
        payload = {
            "count": str(count),
            "interval": {"startTime": start.isoformat(), "endTime": end.isoformat()},
        }
        rows.append(raw("steps", gid, start, payload, source, end, **flags))
    return rows


def sleep(gid, wake, source=BAND):
    start = wake - timedelta(hours=8)
    payload = {
        "interval": {"startTime": start.isoformat(), "endTime": wake.isoformat()},
        "summary": {"minutesAsleep": 480},
    }
    return [
        SleepSession(google_data_point_id=gid, start_time=start, end_time=wake, awake_seconds=0),
        raw("sleep", gid, start, payload, source, wake),
    ]


def daily(kind, gid, day, payload, source=BAND, **flags):
    return raw(kind, gid, at(day), {"date": day.isoformat(), **payload}, source, **flags)


def weight(gid, when, kilograms=70, source=BAND):
    return [
        Weight(google_data_point_id=gid, timestamp=when, kilograms=kilograms),
        raw(
            "weight",
            gid,
            when,
            {
                "weightGrams": kilograms * 1000,
                "sampleTime": {"physicalTime": when.isoformat()},
            },
            source,
            when,
        ),
    ]


def full_day(day: date, prefix: str = "d") -> list:
    """Every dashboard scalar present for ``day``; steps cover ten minutes only."""
    return [
        *steps(f"{prefix}-steps-{day}", at(day, 7)),
        *sleep(f"{prefix}-sleep-{day}", at(day, 6)),
        daily("daily-resting-heart-rate", f"{prefix}-rhr-{day}", day, {"beatsPerMinute": 60}),
        daily(
            "daily-heart-rate-variability",
            f"{prefix}-hrv-{day}",
            day,
            {"averageHeartRateVariabilityMilliseconds": 40},
        ),
        daily("daily-oxygen-saturation", f"{prefix}-spo2-{day}", day, {"averagePercentage": 97}),
    ]


def coverage_of(card, key):
    return next(metric for metric in card["metrics"] if metric["metric_key"] == key)


@pytest.fixture
def archive(sessions, monkeypatch):
    monkeypatch.setattr(codex_health, "SessionLocal", sessions)

    async def add(*rows):
        async with sessions() as session:
            session.add_all(rows)
            await session.commit()

    return add


def query(start, end, **extra):
    return {"action": "coverage", "start_date": str(start), "end_date": str(end), **extra}


def test_schema_root_and_existing_actions_are_kept():
    schema = ArchiveQuery.model_json_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["action"]
    assert schema["properties"]["action"]["enum"][:4] == [
        "inventory",
        "daily",
        "records",
        "coverage",
    ]
    assert HEALTH_TOOL["inputSchema"] == schema
    assert "coverage tells, per metric" in HEALTH_TOOL["description"]
    assert "No SQL or writes allowed." in HEALTH_TOOL["description"]
    ArchiveQuery.model_validate(query("2025-10-01", "2026-09-30"))
    ArchiveQuery.model_validate(
        {"action": "daily", "start_date": "2025-10-01", "end_date": "2026-09-30"}
    )
    for arguments in (
        query("2025-09-29", "2026-09-30"),  # 367 days
        query("2099-01-01", "2099-01-02"),
        {"action": "coverage"},
        {
            "action": "records",
            "data_type": "steps",
            "start_date": "2026-08-01",
            "end_date": "2026-09-30",
        },
    ):
        with pytest.raises(ValidationError):
            ArchiveQuery.model_validate(arguments)


async def test_empty_archive_has_no_values_and_unknown_retrieval(archive):
    card = await read_health(query("2026-09-01", "2026-09-07"))
    assert card["schema_version"] == "1"
    assert (card["requested_days"], card["closed_days"]) == (7, 7)
    assert "retrieval_receipts_unavailable" in card["reason_codes"]
    assert "today_partial_excluded" not in card["reason_codes"]
    assert [metric["metric_key"] for metric in card["metrics"]] == [
        "steps", "sleep_seconds", "resting_bpm", "hrv_ms", "spo2",
        "weight", "height", "body_fat", "muscle",
    ]  # fmt: skip
    for metric in card["metrics"]:
        assert metric["value_days"] == 0
        assert metric["days_without_value"] == 7  # no value, not zero steps
        assert metric["partial_today"] is False
        assert metric["source_regimes"] == []
        assert metric["sampling"] == {
            "basis": "presence_only",
            "sample_count": 0,
            "supported_seconds": None,
            "target_seconds": None,
            "longest_gap_seconds": None,
        }
        assert metric["retrieval"]["state"] == "unknown"
        assert metric["retrieval"]["completed_empty_days"] is None
        assert metric["retrieval"]["unknown_days"] == 7
        assert {"no_value_in_window", "retrieval_unknown"} <= set(metric["reason_codes"])
    weight = coverage_of(card, "weight")
    assert weight["exclusions"]["manually_excluded"] == 0
    assert "manual_exclusion_unavailable" not in weight["reason_codes"]
    assert coverage_of(card, "steps")["exclusions"]["invalid_shape"] is None


async def test_short_step_day_called_complete_is_presence_only(archive):
    day = date(2026, 9, 29)
    await archive(*full_day(day))
    legacy = await read_health(
        {"action": "daily", "start_date": str(day), "end_date": str(day), "timezone": "UTC"}
    )
    assert legacy["days"][0]["steps"] == 12
    assert legacy["days"][0]["status"] == "complete"  # unchanged legacy label
    card = await read_health(query(day, day))
    assert "daily_status_not_coverage" in card["reason_codes"]
    steps_coverage = coverage_of(card, "steps")
    assert steps_coverage["value_days"] == 1
    assert steps_coverage["sampling"]["basis"] == "presence_only"
    assert steps_coverage["sampling"]["supported_seconds"] is None
    assert "presence_only" in steps_coverage["reason_codes"]
    for key in ("sleep_seconds", "resting_bpm", "hrv_ms", "spo2"):
        assert coverage_of(card, key)["value_days"] == 1
    assert all("status" not in metric for metric in card["metrics"])  # no legacy label


async def test_today_is_partial_and_not_a_closed_day(archive):
    today = datetime.now(UTC).date()
    yesterday = today - timedelta(days=1)
    await archive(*steps("yesterday", at(yesterday, 9)), *steps("today", at(today)))
    card = await read_health(query(yesterday, today))
    assert (card["requested_days"], card["closed_days"]) == (2, 1)
    assert "today_partial_excluded" in card["reason_codes"]
    steps_coverage = coverage_of(card, "steps")
    assert steps_coverage["value_days"] == 1
    assert steps_coverage["days_without_value"] == 0
    assert steps_coverage["partial_today"] is True
    assert steps_coverage["retrieval"]["unknown_days"] == 1
    [regime] = steps_coverage["source_regimes"]
    assert (regime["value_days"], regime["partial_today"]) == (1, True)
    assert regime["last_date"] == str(today)
    assert coverage_of(card, "sleep_seconds")["partial_today"] is False


async def test_two_source_streams_stay_separate_rows(archive):
    first = date(2026, 9, 1)
    days = [first + timedelta(days=i) for i in range(4)]
    await archive(
        *(row for day in days[:3] for row in steps(f"band-{day}", at(day, 8), source=BAND)),
        *(row for day in days[1:] for row in steps(f"phone-{day}", at(day, 8), source=PHONE)),
        *weight("google-weight", at(days[0], 7)),
        ManualMeasurement(metric="weight", value=71, measured_at=at(days[2], 7)),
    )
    card = await read_health(query(days[0], days[-1]))
    steps_coverage = coverage_of(card, "steps")
    assert steps_coverage["value_days"] == 4
    assert "separate_source_streams" in steps_coverage["reason_codes"]
    band, phone = steps_coverage["source_regimes"]
    assert (band["device"], band["form_factor"], band["platform"]) == (
        "Example Band",
        "FITNESS_BAND",
        "FITBIT",
    )
    assert (band["first_date"], band["last_date"], band["value_days"]) == (
        str(days[0]),
        str(days[2]),
        3,
    )
    assert (phone["device"], phone["form_factor"], phone["platform"]) == (
        None,
        "PHONE",
        "HEALTH_KIT",
    )
    assert (phone["first_date"], phone["last_date"], phone["value_days"]) == (
        str(days[1]),
        str(days[3]),
        3,
    )
    assert band["source"] != phone["source"]
    assert band["source"].startswith("kaynak-") and "Example" not in band["source"]
    weight_coverage = coverage_of(card, "weight")
    assert weight_coverage["value_days"] == 2
    assert [(r["origin"], r["value_days"]) for r in weight_coverage["source_regimes"]] == [
        ("google", 1),
        ("manual", 1),
    ]


async def test_exclusions_are_counted_once_and_left_out(archive):
    day = date(2026, 9, 10)
    old, holder = steps("old", at(day, 8)), steps("holder", at(day, 9))
    await archive(
        *steps("live", at(day, 7)),
        *steps("raw-quarantined", at(day + timedelta(days=1), 7), is_quarantined=True),
        *steps("norm-quarantined", at(day + timedelta(days=2), 7), norm_quarantined=True),
        *steps("deleted", at(day + timedelta(days=3), 7), source_deleted_at=SEEN),
        *steps("orphan", at(day + timedelta(days=4), 7), raw_row=False),
        *old,
        *holder,
        daily("daily-heart-rate-variability", "hrv-live", day, {"rmssd": 41}),
        daily(
            "daily-heart-rate-variability",
            "hrv-q",
            day + timedelta(days=1),
            {"rmssd": 50},
            is_quarantined=True,
        ),
        daily(
            "daily-heart-rate-variability",
            "hrv-deleted",
            day + timedelta(days=2),
            {"rmssd": 50},
            source_deleted_at=SEEN,
        ),
        daily("daily-heart-rate-variability", "hrv-shapeless", day + timedelta(days=3), {}),
    )
    await archive(
        RawHealthDataSupersession(
            old_raw_id=old[1].id,
            new_raw_id=holder[1].id,
            data_type="steps",
            old_google_data_point_id="old",
            new_google_data_point_id="holder",
            reason="duplicate",
            old_fingerprint="a" * 64,
            new_fingerprint="a" * 64,
            same_reading=True,
            new_payload_hash="c" * 64,
        )
    )
    card = await read_health(query(day, day + timedelta(days=6)))
    steps_coverage = coverage_of(card, "steps")
    assert steps_coverage["exclusions"] | {"policy": None} == {
        "policy": None,
        "quarantined": 2,
        "without_raw_record": 1,
        "source_deleted": 1,
        "superseded": 1,
        "manually_excluded": 0,
        "invalid_shape": None,
    }
    assert steps_coverage["value_days"] == 1  # only the live row and the holder's day
    assert steps_coverage["sampling"]["sample_count"] == 2
    hrv = coverage_of(card, "hrv_ms")
    assert hrv["value_days"] == 1
    assert hrv["exclusions"]["quarantined"] == 1
    assert hrv["exclusions"]["source_deleted"] == 1
    assert hrv["exclusions"]["superseded"] == 0
    assert hrv["exclusions"]["invalid_shape"] == 1
    assert hrv["exclusions"]["policy"].startswith("first_match: quarantined >")
    assert "örtüşmez" in card["note"]


async def test_a_flagged_manual_measurement_is_counted_apart_and_has_no_value(archive, sessions):
    first, second = date(2026, 9, 3), date(2026, 9, 5)
    kept = ManualMeasurement(metric="weight", value=71, measured_at=at(first, 7))
    flagged = ManualMeasurement(metric="weight", value=7.1, measured_at=at(second, 7))
    await archive(kept, flagged)
    async with sessions() as session:
        assert await record_flag(session, flagged.id, "test", True)
        await session.commit()
    weight = coverage_of(await read_health(query(first, second)), "weight")
    assert weight["value_days"] == 1
    assert weight["exclusions"]["manually_excluded"] == 1
    assert "manual_exclusion_unavailable" not in weight["reason_codes"]
    [manual] = weight["source_regimes"]
    assert (manual["origin"], manual["last_date"], manual["record_count"]) == (
        "manual",
        str(first),
        1,
    )
    async with sessions() as session:  # unflagging (another event) brings it back
        assert await record_flag(session, flagged.id, "test", False)
        await session.commit()
    weight = coverage_of(await read_health(query(first, second)), "weight")
    assert (weight["value_days"], weight["exclusions"]["manually_excluded"]) == (2, 0)


async def test_budget_overrun_is_a_structured_error(archive, monkeypatch):
    monkeypatch.setattr(coverage, "COVERAGE_BUDGET_BYTES", 600)
    result = await read_health(query("2026-09-01", "2026-09-30"))
    assert json.loads(json.dumps(result, ensure_ascii=False)) == result
    assert result["code"] == "coverage_budget_exceeded"
    assert result["error"].startswith("Kapsama yanıtı")
    assert result["size_bytes"] > result["budget_bytes"] == 600
    assert result["suggested_range"] == {"start_date": "2026-09-16", "end_date": "2026-09-30"}
    assert "metrics" not in result
    data, success = await codex.run_health_tool(
        {"tool": "health_archive", "arguments": json.dumps(query("2026-09-01", "2026-09-30"))}
    )
    assert success is False
    assert data["code"] == "coverage_budget_exceeded"


async def test_a_typical_year_fits_the_budget(archive):
    end = date(2026, 9, 30)
    days = [end - timedelta(days=i) for i in range(365)]
    rows = []
    for index, day in enumerate(days):
        if index % 9:  # some days without a value
            rows += full_day(day)
        rows += steps(f"phone-{day}", at(day, 12), source=PHONE)
        if index % 7 == 0:
            rows += weight(f"weight-{day}", at(day, 7))
        if index % 30 == 0:
            rows.append(ManualMeasurement(metric="weight", value=70, measured_at=at(day, 8)))
            rows.append(daily("body-fat", f"fat-{day}", day, {"percentage": 18}))
    rows.append(ManualMeasurement(metric="height", value=180, measured_at=at(days[100], 8)))
    await archive(*rows)
    card = await read_health(query(days[-1], end, timezone="Europe/Paris"))
    size = len(json.dumps(card, ensure_ascii=False).encode())
    assert size < coverage.COVERAGE_BUDGET_BYTES, size
    assert card["closed_days"] == 365
    assert coverage_of(card, "steps")["value_days"] == 365
    assert len(coverage_of(card, "steps")["source_regimes"]) == 2
    assert coverage_of(card, "hrv_ms")["value_days"] == 324
    assert coverage_of(card, "weight")["value_days"] == 64  # 53 Google + 13 manual - 2 shared
    assert coverage_of(card, "height")["value_days"] == 1
    # One metric at a time narrows the answer further.
    narrow = await read_health(query(days[-1], end, data_type="daily-oxygen-saturation"))
    assert [metric["metric_key"] for metric in narrow["metrics"]] == ["spo2"]


COVERAGE_PARAGRAPH = (
    "Kişisel yorumdan önce coverage ile ilgili dönemin kapsama, güncellik, ölçüm tanımı ve\n"
    'karşılaştırılabilirlik bilgisini denetle. "Kayıt var"ı "gün eksiksiz" sayma; eksik,\n'
    "geç gelen, dışlanmış veya ölçüm yöntemi değişmiş veriyi ayır.\n"
)
# sha256 of INSTRUCTIONS before the coverage paragraph (with the sleep_assumptions sentence of
# the sleep total change): earlier sentences stay unchanged around the interpretation protocol.
PREVIOUS_INSTRUCTIONS_SHA256 = "7634cb82daad143557f7dba44e0467988e47e164a7716f867800709ce22a382b"


def test_instructions_add_the_coverage_paragraph_and_keep_the_rest():
    assert COVERAGE_PARAGRAPH in codex_chat.INSTRUCTIONS
    previous = codex_chat.INSTRUCTIONS.replace(COVERAGE_PARAGRAPH, "", 1)
    # Issue 46 inserts the health protocol; the pre-existing text remains pinned.
    before, protocol = previous.split("Sağlık yorum protokolü:\n", 1)
    previous = before + "Karşılaştırmalarda" + protocol.split("Karşılaştırmalarda", 1)[1]
    assert hashlib.sha256(previous.encode()).hexdigest() == PREVIOUS_INSTRUCTIONS_SHA256


async def test_relay_answers_a_coverage_call_end_to_end(archive):
    await archive(*full_day(date(2026, 9, 29)))
    browser, rpc = Browser(), RPC()
    task = asyncio.create_task(codex.relay(browser, rpc, "site-thread"))
    try:
        await browser.incoming.put({"text": "Kapsama durumunu incele"})
        assert (await take(rpc.outgoing))["method"] == "turn/start"
        assert (await take(browser.outgoing))["type"] == "working"
        await rpc.incoming.put(
            {
                "id": "tool-1",
                "method": "item/tool/call",
                "params": {
                    "threadId": "site-thread",
                    "tool": "health_archive",
                    "arguments": json.dumps(query("2026-09-28", "2026-09-29")),
                },
            }
        )
        assert (await take(browser.outgoing))["type"] == "working"
        reply = await take(rpc.outgoing)
        assert reply["id"] == "tool-1"
        assert reply["result"]["success"] is True
        card = json.loads(reply["result"]["contentItems"][0]["text"])
        assert card["schema_version"] == "1"
        assert coverage_of(card, "steps")["value_days"] == 1
        assert coverage_of(card, "steps")["days_without_value"] == 1
    finally:
        await browser.incoming.put(WebSocketDisconnect())
        with pytest.raises(WebSocketDisconnect):
            await asyncio.wait_for(task, 2)


async def test_an_invalid_sleep_session_is_no_value_in_every_part_of_the_card(archive):
    day = date(2026, 9, 12)
    wake = at(day, 6)
    await archive(
        SleepSession(google_data_point_id="zero", start_time=wake, end_time=wake, awake_seconds=0),
        raw("sleep", "zero", wake, {}, BAND, wake),
    )
    sleep_coverage = coverage_of(await read_health(query(day, day)), "sleep_seconds")
    assert sleep_coverage["value_days"] == 0
    assert "no_value_in_window" in sleep_coverage["reason_codes"]
    assert sleep_coverage["source_regimes"] == []
    assert sleep_coverage["sampling"]["sample_count"] == 0


async def test_both_read_paths_put_a_quarantined_deleted_record_in_one_class(archive):
    day = date(2026, 9, 14)
    await archive(
        *steps("steps-both", at(day, 7), norm_quarantined=True, source_deleted_at=SEEN),
        daily(
            "daily-heart-rate-variability",
            "hrv-both",
            day,
            {"rmssd": 41},
            source_deleted_at=SEEN,
        ),
        HeartRateVariability(
            google_data_point_id="hrv-both", timestamp=at(day), rmssd=41, is_quarantined=True
        ),
    )
    card = await read_health(query(day, day))
    for key in ("steps", "hrv_ms"):
        exclusions = coverage_of(card, key)["exclusions"]
        assert (exclusions["quarantined"], exclusions["source_deleted"]) == (1, 0), key
        assert exclusions["policy"] == coverage.EXCLUSION_POLICY


async def test_an_unknown_coverage_data_type_says_which_are_accepted(archive):
    assert (
        "daily-heart-rate-variability, daily-oxygen-saturation, weight"
        in (HEALTH_TOOL["description"])
    )
    result = await read_health(query("2026-09-01", "2026-09-07", data_type="exercise"))
    assert result["code"] == "coverage_unknown_data_type"
    assert result["data_type"] == "exercise"
    assert {"steps", "sleep", "hrv_ms", "body-fat", "muscle"} <= set(result["accepted_data_types"])
    data, success = await codex.run_health_tool(
        {"tool": "health_archive", "arguments": query("2026-09-01", "2026-09-07", data_type="x")}
    )
    assert success is False
    assert data["code"] == "coverage_unknown_data_type"
    assert data["error"].startswith("coverage için data_type")


@pytest.mark.parametrize(
    "zone, minutes", [("Europe/Paris", 1320), ("Asia/Kolkata", 1110), ("America/New_York", 240)]
)
@pytest.mark.parametrize("fields", INVALID_FIELDS)
async def test_weight_value_days_follow_current_payload_across_midnight(
    archive, sessions, zone, minutes, fields
):
    from zoneinfo import ZoneInfo

    from derived_records import T0, snapshot, store
    from weight_records import correction, weight

    left, right = T0 + timedelta(minutes=minutes - 1), T0 + timedelta(minutes=minutes + 1)
    first, last = left.astimezone(ZoneInfo(zone)).date(), right.astimezone(ZoneInfo(zone)).date()
    for record, expected in (
        (weight(at=left), first),
        (correction(right, fields), None),
        (weight(grams=73000, at=right), last),
    ):
        await store(sessions, "weight", record)
        async with sessions() as session:
            before = await snapshot(session)
        for day in (first, last):
            card = coverage_of(await read_health(query(day, day, timezone=zone)), "weight")
            assert card["value_days"] == int(day == expected)
            assert card["sampling"]["sample_count"] == int(day == expected)
            assert card["exclusions"]["invalid_shape"] is None  # schema-1 stays unknown
            assert len(card["source_regimes"]) == int(day == expected)
            if day == expected:
                [stream] = card["source_regimes"]
                assert stream["first_date"] == stream["last_date"] == str(day)
                assert stream["record_count"] == 1
        async with sessions() as session:
            assert await snapshot(session) == before
