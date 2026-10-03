from decimal import Decimal

from app.google_health.data_types import REGISTRY
from app.models.health import Distance, SleepSession, Steps, Weight
from app.parsers.normalize import normalize


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
