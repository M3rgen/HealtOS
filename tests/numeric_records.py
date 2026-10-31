"""Synthetic numeric payloads spanning every normalized Numeric column."""

from copy import deepcopy

from derived_records import store as archive
from retrieval_records import point
from sqlalchemy import update

from app.google_health.data_types import REGISTRY
from app.models.health import Steps

BAD_NUMBERS = ("sNaN", "NaN", "Infinity", "-Infinity", "1e2000000", "1e99999999999999999999")
# (data type, payload path, normalized column, source multiplier)
NUMERIC_FIELDS = [
    ("heart-rate", "beatsPerMinute", "bpm", 1),
    ("heart-rate-variability", "rmssd", "rmssd", 1),
    ("oxygen-saturation", "percentage", "spo2", 1),
    ("steps", "count", "count", 1),
    ("sleep", "duration", "duration_seconds", 1),
    *(("sleep", f"stages.{s}", f"{s}_sleep_seconds", 1) for s in ("deep", "light", "rem")),
    ("sleep", "stages.awake", "awake_seconds", 1),
    ("sleep", "summary.minutesAsleep", "efficiency", "0.01"),
    ("exercise", "activeDuration", "duration_seconds", 1),
    ("exercise", "metricsSummary.caloriesKcal", "calories_kcal", 1),
    ("exercise", "metricsSummary.distanceMillimeters", "distance_meters", 1000),
    ("daily-resting-heart-rate", "beatsPerMinute", "bpm", 1),
    ("vo2-max", "vo2Max", "ml_per_kg_min", 1),
    ("core-body-temperature", "temperatureCelsius", "celsius", 1),
    ("core-body-temperature", "baselineTemperatureCelsius", "baseline_celsius", 1),
    ("daily-respiratory-rate", "breathsPerMinute", "breaths_per_minute", 1),
    ("active-zone-minutes", "activeZoneMinutes", "minutes", 1),
    ("distance", "millimeters", "meters", 1000),
    ("total-calories", "kcalSum", "kcal", 1),
    ("weight", "weightGrams", "kilograms", 1000),
    ("body-fat", "percentage", "value", 1),
]


def numeric_point(kind="weight", path="weightGrams", value="72500"):
    record = point(kind=kind)
    payload = record[REGISTRY[kind].json_key]
    payload.update(
        interval={"startTime": "2026-09-01T12:00:00Z", "endTime": "2026-09-01T13:00:00Z"},
        temperatureCelsius="36.5",
        summary={"minutesInSleepPeriod": "1"},
    )
    if path.startswith("stages."):
        payload["stages"] = [{"stage": path.split(".")[1], "duration": value}]
    else:
        keys = path.split(".")
        for key in keys[:-1]:
            payload = payload.setdefault(key, {})
        payload[keys[-1]] = value
    return record


async def store_read_correction(sessions, kind, *records):
    # Exercise read arithmetic on a current raw correction while a previous valid
    # normalized row remains. New ingestion no longer writes out-of-range SQL values.
    if kind in {"steps", "heart-rate", "sleep"}:
        seeds = deepcopy(records)
        for record in seeds:
            payload = record[REGISTRY[kind].json_key]
            if kind == "sleep":
                for key in ("duration", "stages", "sleepStages", "summary"):
                    payload.pop(key, None)
            else:
                payload["count" if kind == "steps" else "beatsPerMinute"] = 1
        await archive(sessions, kind, *seeds)
    return await archive(sessions, kind, *records)


async def store_legacy_steps(sessions, kind, *records):
    # SQLite-only read fixtures deliberately exceed PostgreSQL Numeric. Keep the
    # historical arithmetic regression independent of the stricter ingestion path.
    await store_read_correction(sessions, kind, *records)
    async with sessions.begin() as session:
        for record in records:
            await session.execute(
                update(Steps)
                .where(Steps.google_data_point_id == record["name"])
                .values(count=record["steps"]["count"])
            )
