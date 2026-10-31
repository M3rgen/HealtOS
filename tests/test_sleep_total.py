"""Asleep time by source shape, invalid sessions and visible sleep assumptions."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, Overflow, Underflow, localcontext
from itertools import pairwise
from random import Random

import httpx
import pytest
from sleep_numeric_records import BAD_SLEEP_NUMBERS, SLEEP_FIELDS, UNUSED_SLEEP_FIELDS, evidence

from app.db.session import get_session
from app.google_health.data_types import REGISTRY
from app.main import create_app
from app.models.health import SleepSession
from app.models.state import RawHealthData
from app.parsers.normalize import normalize
from app.parsers.sleep import SLEEP_NUMERIC_ERROR, _sleep_stages, sleep_asleep_seconds
from app.services.canonical_sleep import sleep_evidence
from app.services.current_sleep import current_sleep
from app.services.dashboard import dashboard_data, sleep_estimate, sleep_total
from app.services.storage import store_records

PREFIX = "users/me/dataTypes/sleep/dataPoints"
NIGHT = {"startTime": "2026-09-28T22:00:00Z", "endTime": "2026-09-29T06:00:00Z"}  # 480 min


def test_sweep_matches_original_cell_scan():
    rng = Random(123)
    for size in range(40):
        pairs, spans, found = [], [], set()
        for _ in range(size):
            start = datetime(2026, 9, 29, tzinfo=UTC) + timedelta(minutes=rng.randrange(20))
            minutes, fraction = rng.randrange(8), rng.choice([0, 0.25, 0.5, 1])
            end = start + timedelta(minutes=minutes)
            row = session(
                start.isoformat(), end.isoformat(), minutes * 60, minutes * 60 * (1 - fraction)
            )
            payload = (
                {"summary": {"minutesAsleep": minutes * fraction}} if rng.randrange(2) else None
            )
            pairs.append((row, payload))
            if not minutes:
                found.add("invalid_sessions_skipped")
                continue
            if payload is None:
                found.add("from_normalized_columns")
            spans.append((start, end, fraction))
        total = 0.0
        edges = sorted({edge for a, b, _ in spans for edge in (a, b)})
        for left, right in pairwise(edges):
            fractions = [f for a, b, f in spans if a < right and b > left]
            if fractions:
                total += (right - left).total_seconds() * min(fractions)
                if len(fractions) > 1 and min(fractions) < 1:
                    found.add("overlap_estimated")
        rng.shuffle(pairs)
        estimate = sleep_estimate(pairs)
        assert estimate.seconds == (total if spans else None)
        assert set(estimate.assumptions) == found


def instant(value):
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def stage(kind, start, end):
    return {"type": kind, "startTime": f"2026-09-{start}Z", "endTime": f"2026-09-{end}Z"}


CLASSIC = {
    "interval": NIGHT,
    "type": "CLASSIC",
    "summary": {
        "minutesInSleepPeriod": "480",
        "minutesAsleep": "420",
        "minutesAwake": "30",
        "stagesSummary": [
            {"type": "ASLEEP", "minutes": "420"},
            {"type": "RESTLESS", "minutes": "30"},
            {"type": "AWAKE", "minutes": "30"},
        ],
    },
}
RESTLESS_STAGES = {
    "interval": NIGHT,
    "type": "CLASSIC",
    "stages": [
        stage("ASLEEP", "28T22:00:00", "29T02:00:00"),
        stage("RESTLESS", "29T02:00:00", "29T02:20:00"),
        stage("AWAKE", "29T02:20:00", "29T02:30:00"),
        stage("ASLEEP", "29T02:30:00", "29T06:00:00"),
    ],
}


def record(name, payload):
    return {"name": f"{PREFIX}/{name}", "sleep": payload}


def session(start, end, duration=None, awake=None):
    return SleepSession(
        start_time=instant(start),
        end_time=instant(end),
        duration_seconds=duration,
        awake_seconds=awake,
    )


@pytest.mark.parametrize(
    "payload,seconds,assumptions",
    [
        # Google's own figure wins over stages: CLASSIC minutesAsleep leaves out RESTLESS.
        (CLASSIC, 420 * 60, ()),
        (
            {**CLASSIC, "summary": {"stagesSummary": CLASSIC["summary"]["stagesSummary"]}},
            420 * 60,
            (),
        ),
        (RESTLESS_STAGES, 480 * 60 - 20 * 60 - 10 * 60, ()),
        # A measured zero awake is a measurement: the whole period asleep, no assumption.
        (
            {"interval": NIGHT, "summary": {"stagesSummary": [{"type": "AWAKE", "minutes": "0"}]}},
            480 * 60,
            (),
        ),
        ({"interval": NIGHT, "stages": [{"stage": "wake", "duration": "600s"}]}, 470 * 60, ()),
        ({"interval": NIGHT}, None, ("awake_unknown",)),
        # The top-level duration has no Google schema meaning; kept as normalize takes it.
        (
            {
                "interval": NIGHT,
                "duration": "27000s",
                "stages": [{"stage": "awake", "duration": "1800s"}],
            },
            27000 - 1800,
            ("duration_from_payload",),
        ),
        (
            {"interval": NIGHT, "duration": "27000s"},
            None,
            ("duration_from_payload", "awake_unknown"),
        ),
    ],
)
def test_asleep_rule_by_source_shape(payload, seconds, assumptions):
    result = sleep_asleep_seconds(
        payload, instant(NIGHT["startTime"][:-1]), instant(NIGHT["endTime"][:-1])
    )
    assert result.seconds == (None if seconds is None else Decimal(seconds))
    assert result.assumptions == assumptions
    assert result.period == Decimal(27000 if "duration" in payload else 28800)


@pytest.mark.parametrize(
    "summary",
    [
        {
            "summary": {
                "stagesSummary": [
                    {"type": "LIGHT", "minutes": "480"},
                    {"type": "AWAKE", "minutes": "0"},
                ]
            }
        },
        {
            "stages": [
                {"stage": "light", "duration": "28800s"},
                {"stage": "awake", "duration": "0s"},
            ]
        },
    ],
)
def test_normalizer_keeps_a_measured_zero_awake(summary):
    result = normalize(REGISTRY["sleep"], {"sleep": {"interval": NIGHT, **summary}})
    assert result is not None
    assert result.values["awake_seconds"] == 0
    assert (
        normalize(REGISTRY["sleep"], {"sleep": {"interval": NIGHT}}).values["awake_seconds"] is None
    )


def test_invalid_sessions_are_no_measured_zero():
    invalid = [
        session("2026-09-29T06:00:00", "2026-09-29T06:00:00", 0, 0),
        session("2026-09-29T06:00:00", "2026-09-29T05:00:00", 3600, 0),
    ]
    assert sleep_total(invalid) is None
    assert sleep_estimate([(row, None) for row in invalid]).assumptions == [
        "invalid_sessions_skipped"
    ]
    assert sleep_total([]) is None
    assert sleep_estimate([]).assumptions == []

    valid = session("2026-09-28T22:00:00", "2026-09-29T06:00:00", 28800, 1800)
    mixed = sleep_estimate([(valid, None), *((row, None) for row in invalid)])
    assert mixed.seconds == 27000
    assert mixed.assumptions == ["invalid_sessions_skipped", "from_normalized_columns"]


@pytest.mark.parametrize(
    "payload",
    [
        {"interval": NIGHT, "summary": {"stagesSummary": [{"type": "ASLEEP", "minutes": "450"}]}},
        {
            "interval": NIGHT,
            "stages": [
                stage("LIGHT", "28T22:00:00", "29T02:00:00"),
                stage("DEEP", "29T02:00:00", "29T03:30:00"),
                stage("REM", "29T03:30:00", "29T06:00:00"),
            ],
        },
    ],
)
def test_stages_without_an_awake_entry_mark_awake_unknown(payload):
    """No AWAKE entry is not a measured zero: the value stays the upper bound, and the
    raw path marks it as the normalized-column path does."""
    start, end = instant(NIGHT["startTime"][:-1]), instant(NIGHT["endTime"][:-1])
    result = sleep_asleep_seconds(payload, start, end)
    assert (result.seconds, result.assumptions) == (Decimal(28800), ("awake_unknown",))
    row = session(NIGHT["startTime"][:-1], NIGHT["endTime"][:-1], 28800, None)
    from_raw = sleep_estimate([(row, payload)])
    assert (from_raw.seconds, from_raw.assumptions) == (28800, ["awake_unknown"])
    restless = {
        **payload,
        "stages": [*payload.get("stages", []), stage("RESTLESS", "29T06:00:00", "29T06:20:00")],
    }
    if "summary" in payload:
        restless = {
            "interval": NIGHT,
            "summary": {
                "stagesSummary": [
                    {"type": "ASLEEP", "minutes": "450"},
                    {"type": "RESTLESS", "minutes": "20"},
                ]
            },
        }
    assert sleep_asleep_seconds(restless, start, end).seconds == Decimal(28800 - 1200)


def test_unknown_awake_is_the_period_marked_as_upper_bound():
    row = session(NIGHT["startTime"][:-1], NIGHT["endTime"][:-1], 28800, None)
    from_raw = sleep_estimate([(row, {"interval": NIGHT})])
    assert (from_raw.seconds, from_raw.assumptions) == (28800, ["awake_unknown"])
    from_columns = sleep_estimate([(row, None)])
    assert (from_columns.seconds, from_columns.assumptions) == (
        28800,
        ["from_normalized_columns", "awake_unknown"],
    )


@pytest.mark.parametrize("value", BAD_SLEEP_NUMBERS)
@pytest.mark.parametrize("field", SLEEP_FIELDS)
def test_invalid_selected_numbers_never_become_full_nights_or_measured_zero(field, value):
    row = session(NIGHT["startTime"][:-1], NIGHT["endTime"][:-1], 28800, 1800)
    payload = {"interval": NIGHT, **evidence(field, value)}
    # Even permissive caller contexts cannot suppress the sleep validation boundary.
    with localcontext() as context:
        context.prec = 2
        for signal in (InvalidOperation, Overflow, Underflow):
            context.traps[signal] = False
        measured = sleep_asleep_seconds(payload, row.start_time, row.end_time)
        assert measured.seconds is measured.period is None
        assert measured.assumptions == (SLEEP_NUMERIC_ERROR,)
        estimate = sleep_estimate([(row, payload)])
        canonical, timeline = sleep_evidence([(row, payload)])
    assert estimate == canonical
    assert estimate.seconds is None and estimate.assumptions == [SLEEP_NUMERIC_ERROR]
    assert any(
        code.startswith("unsupported_or_missing_value:") for code in timeline["reason_codes"]
    )


@pytest.mark.parametrize("value", BAD_SLEEP_NUMBERS)
@pytest.mark.parametrize("field", UNUSED_SLEEP_FIELDS)
def test_unused_invalid_stage_totals_leave_the_awake_subtraction(field, value):
    payload = evidence(field, value)
    entries = payload.get("summary", {}).get("stagesSummary") or payload["stages"]
    entries.append({"type": "AWAKE", "minutes": "30", "duration": "1800s"})
    row = session(NIGHT["startTime"][:-1], NIGHT["endTime"][:-1])
    measured = sleep_asleep_seconds(payload, row.start_time, row.end_time)
    assert (measured.seconds, measured.assumptions) == (Decimal(27000), ())


@pytest.mark.parametrize("field", SLEEP_FIELDS)
def test_missing_numbers_remain_distinct_from_invalid_evidence(field):
    payload = evidence(field, "0")
    # Zero values, including a zero duration, are measurements rather than missing fields.
    row = session(NIGHT["startTime"][:-1], NIGHT["endTime"][:-1])
    measured = sleep_asleep_seconds(payload, row.start_time, row.end_time)
    estimate = sleep_estimate([(row, payload)])
    assert SLEEP_NUMERIC_ERROR not in measured.assumptions
    assert estimate.seconds == (0 if field in {"summary", "duration"} else 28800)
    missing = sleep_asleep_seconds({}, row.start_time, row.end_time)
    assert missing.seconds is None and missing.period == 28800
    assert missing.assumptions == ("awake_unknown",)


@pytest.mark.parametrize("value", BAD_SLEEP_NUMBERS)
def test_bad_stage_cannot_hide_behind_valid_stage_or_fall_back_to_timestamps(value):
    payload = evidence("stage_awake", value)
    payload["stages"].append({"type": "AWAKE", "duration": "0s"})
    totals = _sleep_stages(payload)
    assert not totals["awake"].is_finite()
    assert normalize(REGISTRY["sleep"], record("bad", {"interval": NIGHT, **payload})) is None


@pytest.mark.parametrize("column", ["duration_seconds", "awake_seconds"])
@pytest.mark.parametrize("value", BAD_SLEEP_NUMBERS)
def test_legacy_columns_are_validated_before_subtraction_and_clamping(column, value):
    row = session(NIGHT["startTime"][:-1], NIGHT["endTime"][:-1], 28800, 1800)
    setattr(row, column, value)
    estimate = sleep_estimate([(row, None)])
    assert estimate.seconds is None
    assert estimate.assumptions == [SLEEP_NUMERIC_ERROR, "from_normalized_columns"]


def test_finite_operands_with_unrepresentable_sum_or_difference_are_not_clamped():
    row = session(
        NIGHT["startTime"][:-1], NIGHT["endTime"][:-1], Decimal("1e308"), Decimal("-1e308")
    )
    payloads = [
        {"duration": "1e308s", "stages": [{"type": "AWAKE", "duration": "-1e308s"}]},
        {"stages": [{"type": "AWAKE", "duration": "9e307s"}] * 2},
        None,
    ]
    for payload in payloads:
        assert sleep_estimate([(row, payload)]).seconds is None


@pytest.mark.parametrize("same_interval", [True, False])
def test_bad_evidence_makes_the_whole_day_unknown_even_beside_sound_sessions(same_interval):
    bad = session(NIGHT["startTime"][:-1], NIGHT["endTime"][:-1])
    good = bad if same_interval else session("2026-09-29T13:00:00", "2026-09-29T14:00:00")
    pairs = [
        (bad, {"summary": {"minutesAsleep": "NaN"}}),
        (good, {"summary": {"minutesAsleep": 0}}),
    ]
    estimate = sleep_estimate(pairs)
    assert estimate.seconds is None and estimate.assumptions == [SLEEP_NUMERIC_ERROR]
    assert sleep_evidence(pairs)[0] == estimate


@pytest.mark.parametrize("value", ["0e2000000000", "0e-2000000000", "0", "-0"])
def test_measured_zero_summary_survives_extreme_zero_exponents(value):
    row = session(NIGHT["startTime"][:-1], NIGHT["endTime"][:-1])
    estimate = sleep_estimate([(row, {"summary": {"minutesAsleep": value}})])
    assert estimate.seconds == 0 and estimate.assumptions == []


def test_nonzero_asleep_fraction_underflow_is_not_a_measured_zero():
    row = session(NIGHT["startTime"][:-1], NIGHT["endTime"][:-1])
    estimate = sleep_estimate([(row, {"summary": {"minutesAsleep": "1e-325"}})])
    assert estimate.seconds is None and estimate.assumptions == [SLEEP_NUMERIC_ERROR]


def test_current_top_level_interval_does_not_reuse_old_awake_columns():
    start, end, payload = current_sleep({**NIGHT, "sleep": {}})
    row = session(NIGHT["startTime"][:-1], NIGHT["endTime"][:-1], 28800, 1800)
    assert (start, end) == (row.start_time, row.end_time)
    estimate = sleep_estimate([(row, payload)])
    assert (estimate.seconds, estimate.assumptions) == (28800, ["awake_unknown"])


def test_overlap_estimate_is_marked_only_when_it_guesses():
    night = session("2026-09-28T22:00:00", "2026-09-29T06:00:00", 28800, 0)
    inner = session("2026-09-28T23:00:00", "2026-09-29T05:00:00", 21600, 0)
    exact = sleep_estimate(
        [
            (night, {"interval": NIGHT, "stages": [{"stage": "awake", "duration": "0s"}]}),
            (
                inner,
                {
                    "interval": NIGHT,
                    "stages": [
                        {"stage": "light", "duration": "1s"},
                        {"stage": "awake", "duration": "0s"},
                    ],
                },
            ),
        ]
    )
    assert (exact.seconds, exact.assumptions) == (8 * 3600, [])
    awake = sleep_estimate(
        [
            (night, {"interval": NIGHT, "stages": [{"stage": "awake", "duration": "2880s"}]}),
            (inner, None),
        ]
    )
    # 90 % asleep over the overlap and the rest of the night.
    assert awake.seconds == pytest.approx(0.9 * 8 * 3600)
    assert awake.assumptions == ["from_normalized_columns", "overlap_estimated"]


async def test_dashboard_reads_asleep_from_each_sessions_raw_payload(sessions):
    async with sessions.begin() as session_:
        await store_records(
            session_,
            REGISTRY["sleep"],
            [
                record("classic", CLASSIC),
                record(
                    "restless-stages",
                    {
                        **RESTLESS_STAGES,
                        "interval": {
                            "startTime": "2026-09-29T22:00:00Z",
                            "endTime": "2026-09-30T06:00:00Z",
                        },
                    },
                ),
                record(
                    "interval-only",
                    {
                        "interval": {
                            "startTime": "2026-09-30T22:00:00Z",
                            "endTime": "2026-10-01T04:00:00Z",
                        }
                    },
                ),
            ],
        )
    async with sessions() as session_:
        data = await dashboard_data(session_, date(2026, 10, 1), 4, "UTC")
    days = {row["date"]: row for row in data["days"]}
    # The CLASSIC row's columns alone would say 480 - 30 awake = 450 minutes.
    assert days["2026-09-29"]["sleep_seconds"] == 420 * 60
    assert days["2026-09-29"]["sleep_assumptions"] == []
    assert days["2026-09-30"]["sleep_seconds"] == 450 * 60
    assert days["2026-10-01"]["sleep_seconds"] == 6 * 3600
    assert days["2026-10-01"]["sleep_assumptions"] == ["awake_unknown"]
    assert (days["2026-09-28"]["sleep_seconds"], days["2026-09-28"]["sleep_assumptions"]) == (
        None,
        [],
    )
    assert "huzursuz" in data["sleep_method"]


async def test_split_night_and_nap_add_up_on_the_wake_day(sessions):
    payloads = [
        record(
            "first-half",
            {
                "interval": {
                    "startTime": "2026-09-28T22:00:00Z",
                    "endTime": "2026-09-29T01:00:00Z",
                },
                "stages": [
                    stage("LIGHT", "28T22:00:00", "29T00:50:00"),
                    stage("AWAKE", "29T00:50:00", "29T01:00:00"),
                ],
            },
        ),
        record(
            "second-half",
            {
                "interval": {
                    "startTime": "2026-09-29T02:00:00Z",
                    "endTime": "2026-09-29T06:00:00Z",
                },
                "summary": {"minutesAsleep": "230"},
            },
        ),
        record(
            "nap",
            {
                "interval": {
                    "startTime": "2026-09-29T13:00:00Z",
                    "endTime": "2026-09-29T13:40:00Z",
                },
                "summary": {"minutesAsleep": "35"},
            },
        ),
    ]
    async with sessions.begin() as session_:
        await store_records(session_, REGISTRY["sleep"], payloads)
    async with sessions() as session_:
        data = await dashboard_data(session_, date(2026, 9, 29), 1, "UTC")
    day = data["days"][0]
    assert day["sleep_seconds"] == (170 + 230 + 35) * 60
    assert day["sleep_assumptions"] == []


async def test_rows_normalized_before_the_fix_follow_their_raw_payload(sessions):
    """No migration: an old row lost its measured zero awake; its raw payload still has it.
    An unreadable current payload cannot fall back to archived columns."""
    payload = {
        "interval": NIGHT,
        "summary": {
            "stagesSummary": [
                {"type": "LIGHT", "minutes": "480"},
                {"type": "AWAKE", "minutes": "0"},
            ]
        },
    }
    stamp = instant("2026-09-29T07:00:00")
    async with sessions.begin() as session_:
        session_.add_all(
            [
                SleepSession(
                    google_data_point_id="old",
                    start_time=instant("2026-09-28T22:00:00"),
                    end_time=instant("2026-09-29T06:00:00"),
                    duration_seconds=28800,
                    awake_seconds=None,
                ),
                RawHealthData(
                    google_data_point_id="old",
                    data_type="sleep",
                    raw_json={"sleep": payload},
                    source_start_time=instant("2026-09-28T22:00:00"),
                    source_end_time=instant("2026-09-29T06:00:00"),
                    payload_hash="test",
                    first_seen_at=stamp,
                    last_seen_at=stamp,
                ),
                # This session's current payload has no interval ...
                RawHealthData(
                    google_data_point_id="orphan",
                    data_type="sleep",
                    raw_json={},
                    source_start_time=instant("2026-09-29T22:00:00"),
                    source_end_time=instant("2026-09-30T06:00:00"),
                    payload_hash="test",
                    first_seen_at=stamp,
                    last_seen_at=stamp,
                ),
                # ... and the same key under another data type is not its raw record.
                RawHealthData(
                    google_data_point_id="orphan",
                    data_type="exercise",
                    raw_json={"sleep": payload},
                    source_start_time=stamp,
                    payload_hash="test",
                    first_seen_at=stamp,
                    last_seen_at=stamp,
                ),
                SleepSession(
                    google_data_point_id="orphan",
                    start_time=instant("2026-09-29T22:00:00"),
                    end_time=instant("2026-09-30T06:00:00"),
                    duration_seconds=28800,
                    awake_seconds=1800,
                ),
            ]
        )
    async with sessions() as session_:
        data = await dashboard_data(session_, date(2026, 9, 30), 2, "UTC")
    days = {row["date"]: row for row in data["days"]}
    assert (days["2026-09-29"]["sleep_seconds"], days["2026-09-29"]["sleep_assumptions"]) == (
        28800,
        [],
    )
    assert (days["2026-09-30"]["sleep_seconds"], days["2026-09-30"]["sleep_assumptions"]) == (
        None,
        [],
    )


async def test_dashboard_api_and_csv_keep_their_shape(sessions):
    application = create_app()

    async def override_session():
        async with sessions() as session_:
            yield session_

    application.dependency_overrides[get_session] = override_session
    async with sessions.begin() as session_:
        await store_records(session_, REGISTRY["sleep"], [record("night", {"interval": NIGHT})])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://localhost",
        headers={"X-HealthOS-Request": "1"},
    ) as client:
        day = (await client.get("/api/dashboard?end_date=2026-09-29&days=1")).json()["days"][0]
        csv = (await client.get("/api/dashboard/export.csv?end_date=2026-09-29&days=1")).text
    await application.state.runtime.shutdown()
    assert (day["sleep_seconds"], day["sleep_assumptions"]) == (28800, ["awake_unknown"])
    assert (
        csv.splitlines()[0].lstrip("﻿")
        == "date,steps,sleep_seconds,resting_bpm,hrv_ms,spo2,status,timezone"
    )
