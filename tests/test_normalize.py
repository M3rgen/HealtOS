from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from numeric_records import BAD_NUMBERS, NUMERIC_FIELDS, numeric_point
from retrieval_records import BAD_TIMES, OVERFLOW_TIMES, point

from app.google_health.data_types import REGISTRY
from app.models.health import Distance, SleepSession, Steps, Weight
from app.parsers.common import parse_date, parse_datetime, temporal
from app.parsers.identity import record_identity
from app.parsers.normalize import normalize
from app.parsers.sleep import SleepAsleep, sleep_asleep_seconds


def test_steps_parser():
    record = {
        "name": "step-1",
        "steps": {
            "interval": {
                "startTime": "2026-08-01T10:00:00Z",
                "endTime": "2026-08-01T10:01:00Z",
            },
            "count": "42",
        },
    }
    result = normalize(REGISTRY["steps"], record)
    assert result.model is Steps
    assert result.values["count"] == 42


def test_sleep_parser_sums_stages():
    record = {
        "name": "sleep-1",
        "sleep": {
            "interval": {
                "startTime": "2026-08-01T22:00:00Z",
                "endTime": "2026-08-02T06:00:00Z",
            },
            "stages": [
                {"stage": "deep", "duration": "1200s"},
                {"stage": "deep", "duration": "300s"},
                {"stage": "rem", "duration": "900s"},
            ],
        },
    }
    result = normalize(REGISTRY["sleep"], record)
    assert result.model is SleepSession
    assert result.values["deep_sleep_seconds"] == 1500
    assert result.values["rem_sleep_seconds"] == 900


def test_official_sleep_stage_timestamps_and_summary_efficiency():
    record = {
        "name": "sleep-2",
        "sleep": {
            "interval": {
                "startTime": "2026-08-01T22:00:00Z",
                "endTime": "2026-08-02T06:00:00Z",
            },
            "stages": [
                {
                    "type": "DEEP",
                    "startTime": "2026-08-01T22:00:00Z",
                    "endTime": "2026-08-01T22:30:00Z",
                }
            ],
            "summary": {"minutesAsleep": "420", "minutesInSleepPeriod": "480"},
        },
    }
    result = normalize(REGISTRY["sleep"], record)
    assert result.values["deep_sleep_seconds"] == 1800
    assert result.values["efficiency"] == 87.5


def test_dedicated_metric_unit_normalization():
    weight = normalize(
        REGISTRY["weight"],
        {
            "name": "weight-1",
            "weight": {
                "sampleTime": {"physicalTime": "2026-08-01T10:00:00Z"},
                "weightGrams": 72500,
            },
        },
    )
    distance = normalize(
        REGISTRY["distance"],
        {
            "name": "distance-1",
            "distance": {
                "interval": {
                    "startTime": "2026-08-01T10:00:00Z",
                    "endTime": "2026-08-01T10:01:00Z",
                },
                "millimeters": "1500",
            },
        },
    )
    assert weight.model is Weight
    assert weight.values["kilograms"] == 72.5
    assert distance.model is Distance
    assert distance.values["meters"] == 1.5


def test_current_schema_field_names_normalize_to_analytics_tables():
    heart_rate = normalize(
        REGISTRY["heart-rate"],
        {
            "dataPointName": "users/u/dataTypes/heart-rate/dataPoints/reconciled",
            "heartRate": {
                "sampleTime": {"physicalTime": "2026-08-18T10:00:00Z"},
                "beatsPerMinute": "72",
                "metadata": {"motionContext": "RESTING"},
            },
        },
    )
    run_vo2 = normalize(
        REGISTRY["run-vo2-max"],
        {
            "runVo2Max": {
                "sampleTime": {"physicalTime": "2026-08-18T10:00:00Z"},
                "runVo2Max": 48.5,
            }
        },
    )
    total = normalize(
        REGISTRY["total-calories"],
        {
            "startTime": "2026-08-18T10:00:00Z",
            "endTime": "2026-08-18T10:01:00Z",
            "totalCalories": {"kcalSum": 1.25},
        },
    )

    assert heart_rate.values["google_data_point_id"].endswith("/reconciled")
    assert heart_rate.values["motion_context"] == "RESTING"
    assert run_vo2.values["ml_per_kg_min"] == Decimal("48.5")
    assert total.values["kcal"] == Decimal("1.25")


@pytest.mark.parametrize("key", ["metadata", "heartRateMetadata"])
@pytest.mark.parametrize("value", [[], ["RESTING"], {}, 42, 0, True, False, None])
def test_heart_rate_ignores_malformed_metadata_and_context(key, value):
    for metadata in (value, "bad", {"motionContext": value}):
        result = normalize(REGISTRY["heart-rate"], point(**{key: metadata}))
        assert result.values["motion_context"] is None
        assert result.values["bpm"] == 60


@pytest.mark.parametrize(
    "metadata, expected",
    [
        *((value, "ACTIVE") for value in ({}, [], [1], "bad", 42, 0, True, False, None)),
        *(({"motionContext": value}, value) for value in ("RESTING", "")),
        ({"other": 1}, None),
        ({"motionContext": []}, None),
    ],
)
def test_heart_rate_metadata_precedence(metadata, expected):
    record = point(metadata=metadata, heartRateMetadata={"motionContext": "ACTIVE"})
    result = normalize(REGISTRY["heart-rate"], record)
    assert result.values["motion_context"] == expected


@pytest.mark.parametrize(
    "parser, value, expected",
    [
        *((parse_datetime, at, None) for at in OVERFLOW_TIMES),
        (parse_datetime, "0001-01-01T01:00:00+01:00", datetime(1, 1, 1, tzinfo=UTC)),
        (parse_datetime, "9999-12-31T22:59:59.999999-01:00", datetime.max.replace(tzinfo=UTC)),
        (parse_datetime, "2026-09-01T12:00:00+02:00", datetime(2026, 9, 1, 10, tzinfo=UTC)),
        (parse_datetime, "0001-01-01T00:00:00", datetime(1, 1, 1, tzinfo=UTC)),
        (parse_date, BAD_TIMES[-1][1], None),
        (parse_date, dict(year=2026, month=2, day=29), None),
        # One failure boundary covers missing keys, int() limits and calendar overflow.
        (parse_date, dict(year=2026, month=9), None),
        (parse_date, dict(year="1" * 5000, month=1, day=1), None),
        (parse_date, dict(year=1e300, month=1, day=1), None),
        (parse_datetime, "broken", None),
        (parse_datetime, 20260901, None),
        *(
            (parse_date, value, day)
            for day in (date.min, date.max, date(2024, 2, 29), date(2026, 10, 27))
            for value in (day.isoformat(), dict(year=day.year, month=day.month, day=day.day))
        ),
    ],
)
def test_source_date_boundaries(parser, value, expected):
    assert parser(value) == expected
    if parser is parse_date and expected is not None:
        for convert in (int, str, float):
            parts = {key: convert(getattr(expected, key)) for key in ("year", "month", "day")}
            assert parse_date(parts) == expected


@pytest.mark.parametrize("key,whole", [("year", 2026), ("month", 9), ("day", 1)])
def test_lossy_dates_produce_no_time_identity_or_normalized_value(key, whole):
    spec = REGISTRY["daily-resting-heart-rate"]
    for value in (True, False, whole + 0.9, *map(float, ("nan", "inf", "-inf"))):
        supplied = {**dict(year=2026, month=9, day=1), key: value}
        record = point(supplied, spec.name, named=False)
        assert parse_date(supplied) is None
        assert temporal(spec, record) == (None, None, None)
        assert record_identity(spec, record) is None
        assert normalize(spec, record) is None


@pytest.mark.parametrize("kind,path,column,multiplier", NUMERIC_FIELDS)
def test_numeric_columns_reject_invalid_values_and_rounding_overflow(
    kind, path, column, multiplier
):
    from decimal import localcontext

    spec = REGISTRY[kind]
    valid = normalize(spec, numeric_point(kind, path, "0"))
    assert valid is not None and valid.values[column] == 0
    sql_type = valid.model.__table__.c[column].type
    optional = valid.model.__table__.c[column].nullable
    with localcontext() as context:
        context.prec = 80
        quantum = Decimal(10) ** -sql_type.scale
        limit = Decimal(10) ** (sql_type.precision - sql_type.scale)
        for sign in (1, -1):
            for offset, accepted in (
                (quantum, True),
                (quantum * Decimal("0.51"), True),
                (quantum / 2, False),
                (Decimal(0), False),
            ):
                value = (limit - offset) * sign * Decimal(str(multiplier))
                result = normalize(spec, numeric_point(kind, path, str(value)))
                # Only a required column's overflow rejects the record; optional is NULL.
                assert (result is not None) == (accepted or optional), (kind, column, value)
                if result is not None:
                    assert result.values[column] == ((limit - quantum) * sign if accepted else None)
    for value in BAD_NUMBERS:
        result = normalize(spec, numeric_point(kind, path, value))
        if result is not None:
            # Finite but unstorable: the optional column alone is dropped.
            assert optional and value == "1e2000000" and result.values[column] is None


@pytest.mark.parametrize(
    "extra",
    [
        {"summary": {"stagesSummary": [{"type": "deep", "minutes": "1e999999"}]}},
        {"stages": [{"stage": "deep", "duration": "6e999999s"}] * 2},
        {"summary": {"minutesAsleep": "NaN", "minutesInSleepPeriod": 1}},
    ],
)
def test_sleep_arithmetic_failures_skip_normalization(extra):
    record = point(kind="sleep", **extra)
    assert normalize(REGISTRY["sleep"], record) is None


@pytest.mark.parametrize(
    "kind,extra,column",
    [
        ("sleep", {"stages": [{"stage": "deep", "duration": "5e10s"}] * 2}, "deep_sleep_seconds"),
        ("sleep", {"summary": {"minutesAsleep": 400, "minutesInSleepPeriod": 1e-4}}, "efficiency"),
        ("sleep", {"summary": {"minutesAsleep": "1e999999"}}, "efficiency"),
        ("exercise", {"metricsSummary": {"caloriesKcal": "1e20"}}, "calories_kcal"),
        ("exercise", {"metricsSummary": {"distanceMillimeters": "1e30"}}, "distance_meters"),
    ],
)
def test_optional_overflow_keeps_the_measurement(kind, extra, column):
    record = numeric_point(kind, "unused", "0")
    payload = record[REGISTRY[kind].json_key]
    payload["summary"].update(extra.pop("summary", {}))
    payload.update(extra)
    result = normalize(REGISTRY[kind], record)
    assert result is not None and result.values[column] is None
    assert result.values["duration_seconds"] == 3600


@pytest.mark.parametrize("value", ["", "abc", "s", {"bad": 1}, []])
def test_sleep_text_numbers_are_missing_and_stage_time_supplies_duration(value):
    """Text is no measurement (#91): a stage without one keeps its interval duration."""
    stages = [{"type": "DEEP", "minutes": value}, {"type": "LIGHT", "minutes": 30}]
    summary = normalize(REGISTRY["sleep"], point(kind="sleep", summary={"stagesSummary": stages}))
    assert summary is not None and summary.values["deep_sleep_seconds"] is None
    assert summary.values["light_sleep_seconds"] == 1800
    stage = {"type": "AWAKE", "startTime": "2026-09-01T12:00:00Z", "duration": value}
    record = point(kind="sleep", stages=[{**stage, "endTime": "2026-09-01T12:10:00Z"}])
    assert normalize(REGISTRY["sleep"], record).values["awake_seconds"] == 600
    start, end = (datetime(2026, 9, 1, hour, tzinfo=UTC) for hour in (12, 13))
    measured = sleep_asleep_seconds(record["sleep"], start, end)
    assert measured == SleepAsleep(Decimal(3000), Decimal(3600), ("duration_from_payload",))
    # Writes and reads share one top-level duration rule: text falls back to the interval.
    record["sleep"]["duration"] = value
    assert normalize(REGISTRY["sleep"], record).values["duration_seconds"] == 3600
    assert sleep_asleep_seconds(record["sleep"], start, end) == SleepAsleep(3000, 3600)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("1e-2000000", "0"),
        ("9999999999.994999999999999999999999", "9999999.99999"),
    ],
)
def test_weight_conversion_keeps_tiny_values_and_exact_rounding(value, expected):
    result = normalize(REGISTRY["weight"], numeric_point(value=value))
    assert result.values["kilograms"] == Decimal(expected)
