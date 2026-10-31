from __future__ import annotations

from dataclasses import dataclass
from decimal import Context, Decimal, DecimalException, localcontext
from typing import Any

from sqlalchemy.orm import DeclarativeBase

from app.google_health.data_types import DataTypeSpec
from app.models.health import (
    ActiveZoneMinute,
    Distance,
    EnergyExpenditure,
    ExerciseSession,
    HeartRate,
    HeartRateVariability,
    OxygenSaturation,
    RespiratoryRate,
    RestingHeartRate,
    ScalarMetric,
    SleepSession,
    Steps,
    Temperature,
    Vo2Max,
    Weight,
)
from app.parsers import numeric
from app.parsers.common import (
    device_name,
    payload_for,
    source_id,
    temporal,
)
from app.parsers.sleep import _sleep_stages, sleep_period_seconds
from app.parsers.text import prepare_optional_text


@dataclass(frozen=True)
class NormalizedRecord:
    model: type[DeclarativeBase]
    values: dict[str, Any]


def _heart_rate_motion_context(payload: dict) -> str | None:
    """Read optional context without rejecting an otherwise usable measurement.

    Nonempty metadata objects retain their original precedence. Empty or malformed
    metadata may fall back to the alternate field; an invalid context inside the
    selected object is absent, rather than being coerced into SQL text.
    """
    for key in ("metadata", "heartRateMetadata"):
        metadata = payload.get(key)
        if not isinstance(metadata, dict) or not metadata:
            continue
        context = metadata.get("motionContext")
        # Google supplies a string enum. Other JSON types are not context labels,
        # even if a database driver happens to accept a scalar as text.
        if isinstance(context, str):
            return context
        return None
    return None


def normalize(spec: DataTypeSpec, record: object) -> NormalizedRecord | None:
    # Only parser/arithmetic failures become skipped normalization. Database writes and
    # connection failures remain outside this boundary and still roll back the page.
    try:
        with localcontext(Context()):
            result = _normalize(spec, record)
            if result is not None:
                numeric.prepare_numeric(result.model, result.values)
                prepare_optional_text(result.model, result.values)
            return result
    except DecimalException:
        return None


def _normalize(spec: DataTypeSpec, record: object) -> NormalizedRecord | None:
    if not isinstance(record, dict):
        return None
    payload = payload_for(spec, record)
    if not payload:
        return None
    start, end, timezone = temporal(spec, record)
    google_id = source_id(spec, record)
    common = {
        "google_data_point_id": google_id,
        "device": device_name(record),
        "source_timezone": timezone,
    }
    if spec.name == "heart-rate" and start:
        bpm = numeric.first_number(payload, ("beatsPerMinute", "bpm", "value"))
        return (
            NormalizedRecord(
                HeartRate,
                {
                    **common,
                    "timestamp": start,
                    "bpm": bpm,
                    "motion_context": _heart_rate_motion_context(payload),
                },
            )
            if bpm is not None
            else None
        )
    if spec.name in {"heart-rate-variability", "daily-heart-rate-variability"} and start:
        rmssd = numeric.first_number(
            payload,
            (
                "rootMeanSquareOfSuccessiveDifferencesMilliseconds",
                "averageHeartRateVariabilityMilliseconds",
                "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds",
                "rmssd",
            ),
        )
        return NormalizedRecord(
            HeartRateVariability,
            {**common, "timestamp": start, "rmssd": rmssd, "metrics": payload},
        )
    if spec.name in {"oxygen-saturation", "daily-oxygen-saturation"} and start:
        spo2 = numeric.first_number(payload, ("percentage", "spo2", "averagePercentage", "average"))
        return (
            NormalizedRecord(OxygenSaturation, {**common, "timestamp": start, "spo2": spo2})
            if spo2 is not None
            else None
        )
    if spec.name == "steps" and start and end:
        count = numeric.first_number(payload, ("count", "countSum", "steps"))
        return (
            NormalizedRecord(
                Steps, {**common, "start_time": start, "end_time": end, "count": count}
            )
            if count is not None
            else None
        )
    if spec.name == "sleep" and start and end:
        stages = _sleep_stages(payload, strict=True)
        return NormalizedRecord(
            SleepSession,
            {
                **common,
                "start_time": start,
                "end_time": end,
                "duration_seconds": sleep_period_seconds(payload, start, end)[0],
                "deep_sleep_seconds": stages.get("deep"),
                "light_sleep_seconds": stages.get("light"),
                "rem_sleep_seconds": stages.get("rem"),
                # A measured zero is kept: only a missing stage is unknown.
                "awake_seconds": stages["awake"] if "awake" in stages else stages.get("wake"),
                "efficiency": _sleep_efficiency(payload),
            },
        )
    if spec.name == "exercise" and start and end:
        metrics = payload.get("metricsSummary", {})
        if not isinstance(metrics, dict):
            metrics = {}
        distance_mm = numeric.first_number(metrics, ("distanceMillimeters",))
        duration = numeric.duration_seconds(payload.get("activeDuration"))
        if duration is None:
            duration = numeric.duration_seconds(payload.get("duration"))
        return NormalizedRecord(
            ExerciseSession,
            {
                **common,
                "start_time": start,
                "end_time": end,
                "exercise_type": payload.get("exerciseType") or payload.get("displayName"),
                "duration_seconds": duration
                if duration is not None
                else Decimal(str((end - start).total_seconds())),
                "calories_kcal": numeric.first_number(metrics, ("caloriesKcal",)),
                "distance_meters": numeric.from_milli(distance_mm)
                if distance_mm is not None
                else None,
            },
        )
    if spec.name == "daily-resting-heart-rate" and start:
        bpm = numeric.number(payload.get("beatsPerMinute"))
        metadata = payload.get("dailyRestingHeartRateMetadata", {})
        return (
            NormalizedRecord(
                RestingHeartRate,
                {
                    **common,
                    "timestamp": start,
                    "bpm": bpm,
                    "calculation_method": metadata.get("calculationMethod")
                    if isinstance(metadata, dict)
                    else None,
                },
            )
            if bpm is not None
            else None
        )
    if spec.name in {"vo2-max", "daily-vo2-max", "run-vo2-max"} and start:
        value = numeric.number(payload.get("runVo2Max" if spec.name == "run-vo2-max" else "vo2Max"))
        return (
            NormalizedRecord(
                Vo2Max,
                {
                    **common,
                    "timestamp": start,
                    "metric_type": spec.name,
                    "ml_per_kg_min": value,
                    "fitness_level": payload.get("cardioFitnessLevel"),
                },
            )
            if value is not None
            else None
        )
    if spec.name in {"core-body-temperature", "daily-sleep-temperature-derivations"} and start:
        value = numeric.first_number(payload, ("temperatureCelsius", "nightlyTemperatureCelsius"))
        return (
            NormalizedRecord(
                Temperature,
                {
                    **common,
                    "timestamp": start,
                    "metric_type": spec.name,
                    "celsius": value,
                    "baseline_celsius": numeric.number(payload.get("baselineTemperatureCelsius")),
                },
            )
            if value is not None
            else None
        )
    if spec.name in {"daily-respiratory-rate", "respiratory-rate-sleep-summary"} and start:
        value = numeric.number(payload.get("breathsPerMinute"))
        full_sleep = payload.get("fullSleepStats", {})
        if value is None and isinstance(full_sleep, dict):
            value = numeric.number(full_sleep.get("breathsPerMinute"))
        return (
            NormalizedRecord(
                RespiratoryRate,
                {
                    **common,
                    "timestamp": start,
                    "metric_type": spec.name,
                    "breaths_per_minute": value,
                },
            )
            if value is not None
            else None
        )
    if spec.name == "active-zone-minutes" and start and end:
        minutes = numeric.number(payload.get("activeZoneMinutes"))
        return (
            NormalizedRecord(
                ActiveZoneMinute,
                {
                    **common,
                    "start_time": start,
                    "end_time": end,
                    "minutes": minutes,
                    "heart_rate_zone": payload.get("heartRateZone"),
                },
            )
            if minutes is not None
            else None
        )
    if spec.name == "distance" and start and end:
        millimeters = numeric.number(payload.get("millimeters"))
        return (
            NormalizedRecord(
                Distance,
                {
                    **common,
                    "start_time": start,
                    "end_time": end,
                    "meters": numeric.from_milli(millimeters),
                },
            )
            if millimeters is not None
            else None
        )
    if spec.name in {"active-energy-burned", "total-calories"} and start and end:
        kcal = numeric.first_number(payload, ("kcal", "kcalSum"))
        return (
            NormalizedRecord(
                EnergyExpenditure,
                {**common, "start_time": start, "end_time": end, "kcal": kcal},
            )
            if kcal is not None
            else None
        )
    if spec.name == "weight" and start:
        grams = numeric.number(payload.get("weightGrams"))
        return (
            NormalizedRecord(
                Weight,
                {**common, "timestamp": start, "kilograms": numeric.from_milli(grams)},
            )
            if grams is not None
            else None
        )
    if start:
        value, unit = _generic_value(payload)
        return NormalizedRecord(
            ScalarMetric,
            {
                **common,
                "data_type": spec.name,
                "start_time": start,
                "end_time": end,
                "value": value,
                "unit": unit,
                "attributes": payload,
            },
        )
    return None


def _sleep_efficiency(payload: dict) -> Decimal | None:
    summary = payload.get("summary", {})
    if not isinstance(summary, dict):
        return None
    asleep = numeric.number(summary.get("minutesAsleep"))
    period = numeric.number(summary.get("minutesInSleepPeriod"))
    if asleep is None or period is None or period == 0:
        return None
    try:
        return (asleep / period) * 100
    except DecimalException:
        # An optional derived ratio must not reject the session it describes.
        return None


def _generic_value(payload: dict) -> tuple[Decimal | None, str | None]:
    ignored = {"year", "month", "day", "hours", "minutes", "seconds", "nanos"}
    candidates = (
        "value",
        "average",
        "count",
        "percentage",
        "kilograms",
        "meters",
        "litersPerMinute",
        "calories",
        "minutes",
    )
    value = numeric.first_number(payload, candidates)
    if value is None:
        for key, candidate in payload.items():
            if key not in ignored and not isinstance(candidate, (dict, list)):
                value = numeric.number(candidate)
                if value is not None:
                    break
    return value, payload.get("unit")
