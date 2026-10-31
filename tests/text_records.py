"""Synthetic optional labels; paths name the original JSON fields, never SQL values."""

from retrieval_records import point

from app.google_health.data_types import REGISTRY

TEXT_FIELDS = [
    ("exercise", "exerciseType", "exercise_type"),
    ("exercise", "displayName", "exercise_type"),
    (
        "daily-resting-heart-rate",
        "dailyRestingHeartRateMetadata.calculationMethod",
        "calculation_method",
    ),
    ("vo2-max", "cardioFitnessLevel", "fitness_level"),
    ("daily-vo2-max", "cardioFitnessLevel", "fitness_level"),
    ("run-vo2-max", "cardioFitnessLevel", "fitness_level"),
    ("active-zone-minutes", "heartRateZone", "heart_rate_zone"),
    ("body-fat", "unit", "unit"),
    *(("heart-rate", f"dataSource.device.{key}", "device") for key in ("displayName", "model")),
    *(
        ("steps", f"interval.{key}", "source_timezone")
        for key in ("startTimeZone", "startUtcOffset")
    ),
    *(("heart-rate", f"sampleTime.{key}", "source_timezone") for key in ("timeZone", "utcOffset")),
    ("daily-resting-heart-rate", "timeZone", "source_timezone"),
]
MISSING = object()
FALLBACKS = {
    "exerciseType": "displayName",
    "dataSource.device.displayName": "model",
    "interval.startTimeZone": "startUtcOffset",
    "sampleTime.timeZone": "utcOffset",
}
BAD_TEXT = ({"bad": 1}, ["bad"], 42, True, False, "ç" * 256)


def text_point(kind, path, value, fallback=None):
    record = point(kind=kind, vo2Max=42, runVo2Max=42, activeZoneMinutes=3, value=42)
    payload = record[REGISTRY[kind].json_key]
    if kind in {"exercise", "active-zone-minutes"}:
        payload["interval"] = point(kind="steps")["steps"]["interval"]
    if kind == "daily-vo2-max":
        payload["date"] = payload.pop("sampleTime")["physicalTime"][:10]
    target = record if path.startswith("dataSource.") else payload
    *parents, key = path.split(".")
    for parent in parents:
        target = target.setdefault(parent, {})
    if value is not MISSING:
        target[key] = value
    if fallback is not None:
        target[FALLBACKS[path]] = fallback
    return record
