from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
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
from app.parsers.common import (
    device_name,
    duration_seconds,
    first_number,
    number,
    parse_datetime,
    payload_for,
    source_id,
    temporal,
)


@dataclass(frozen=True)
class NormalizedRecord:
    model: type[DeclarativeBase]
    values: dict[str, Any]


def normalize(spec: DataTypeSpec, record: object) -> NormalizedRecord | None:
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
        bpm = first_number(payload, ("beatsPerMinute", "bpm", "value"))
        return (
            NormalizedRecord(
                HeartRate,
                {
                    **common,
                    "timestamp": start,
                    "bpm": bpm,
                    "motion_context": (
                        payload.get("metadata") or payload.get("heartRateMetadata") or {}
                    ).get("motionContext"),
                },
            )
            if bpm is not None
            else None
        )
    if spec.name in {"heart-rate-variability", "daily-heart-rate-variability"} and start:
        rmssd = first_number(
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
        spo2 = first_number(payload, ("percentage", "spo2", "averagePercentage", "average"))
        return (
            NormalizedRecord(OxygenSaturation, {**common, "timestamp": start, "spo2": spo2})
            if spo2 is not None
            else None
        )
    if spec.name == "steps" and start and end:
        count = first_number(payload, ("count", "countSum", "steps"))
        return (
            NormalizedRecord(
                Steps, {**common, "start_time": start, "end_time": end, "count": count}
            )
            if count is not None
            else None
        )
    if spec.name == "sleep" and start and end:
        stages = _sleep_stages(payload)
        return NormalizedRecord(
            SleepSession,
            {
                **common,
                "start_time": start,
                "end_time": end,
                "duration_seconds": duration_seconds(payload.get("duration"))
                or Decimal(str((end - start).total_seconds())),
                "deep_sleep_seconds": stages.get("deep"),
                "light_sleep_seconds": stages.get("light"),
                "rem_sleep_seconds": stages.get("rem"),
                "awake_seconds": stages.get("awake") or stages.get("wake"),
                "efficiency": _sleep_efficiency(payload),
            },
        )
    if spec.name == "exercise" and start and end:
        metrics = payload.get("metricsSummary", {})
        if not isinstance(metrics, dict):
            metrics = {}
        distance_mm = first_number(metrics, ("distanceMillimeters",))
        return NormalizedRecord(
            ExerciseSession,
            {
                **common,
                "start_time": start,
                "end_time": end,
                "exercise_type": payload.get("exerciseType") or payload.get("displayName"),
                "duration_seconds": duration_seconds(payload.get("activeDuration"))
                or duration_seconds(payload.get("duration"))
                or Decimal(str((end - start).total_seconds())),
                "calories_kcal": first_number(metrics, ("caloriesKcal",)),
                "distance_meters": distance_mm / 1000 if distance_mm is not None else None,
            },
        )
    if spec.name == "daily-resting-heart-rate" and start:
        bpm = number(payload.get("beatsPerMinute"))
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
        value = number(payload.get("runVo2Max" if spec.name == "run-vo2-max" else "vo2Max"))
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
        value = first_number(payload, ("temperatureCelsius", "nightlyTemperatureCelsius"))
        return (
            NormalizedRecord(
                Temperature,
                {
                    **common,
                    "timestamp": start,
                    "metric_type": spec.name,
                    "celsius": value,
                    "baseline_celsius": number(payload.get("baselineTemperatureCelsius")),
                },
            )
            if value is not None
            else None
        )
    if spec.name in {"daily-respiratory-rate", "respiratory-rate-sleep-summary"} and start:
        value = number(payload.get("breathsPerMinute"))
        full_sleep = payload.get("fullSleepStats", {})
        if value is None and isinstance(full_sleep, dict):
            value = number(full_sleep.get("breathsPerMinute"))
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
        minutes = number(payload.get("activeZoneMinutes"))
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
        millimeters = number(payload.get("millimeters"))
        return (
            NormalizedRecord(
                Distance,
                {
                    **common,
                    "start_time": start,
                    "end_time": end,
                    "meters": millimeters / 1000,
                },
            )
            if millimeters is not None
            else None
        )
    if spec.name in {"active-energy-burned", "total-calories"} and start and end:
        kcal = first_number(payload, ("kcal", "kcalSum"))
        return (
            NormalizedRecord(
                EnergyExpenditure,
                {**common, "start_time": start, "end_time": end, "kcal": kcal},
            )
            if kcal is not None
            else None
        )
    if spec.name == "weight" and start:
        grams = number(payload.get("weightGrams"))
        return (
            NormalizedRecord(
                Weight,
                {**common, "timestamp": start, "kilograms": grams / 1000},
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


def _sleep_stages(payload: dict) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    summary = payload.get("summary", {})
    if isinstance(summary, dict) and isinstance(summary.get("stagesSummary"), list):
        for entry in summary["stagesSummary"]:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("type") or "").lower()
            minutes = number(entry.get("minutes"))
            if name and minutes is not None:
                totals[name] = totals.get(name, Decimal(0)) + (minutes * 60)
        if totals:
            return totals
    entries = payload.get("stages") or payload.get("sleepStages") or []
    if isinstance(entries, dict):
        entries = entries.get("stages") or entries.get("data") or []
    if not isinstance(entries, list):
        return totals
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("stage") or entry.get("type") or "").lower()
        duration = duration_seconds(entry.get("duration") or entry.get("durationSeconds"))
        if duration is None:
            stage_start = parse_datetime(entry.get("startTime"))
            stage_end = parse_datetime(entry.get("endTime"))
            if stage_start and stage_end:
                duration = Decimal(str((stage_end - stage_start).total_seconds()))
        if name and duration is not None:
            totals[name] = totals.get(name, Decimal(0)) + duration
    return totals


def _sleep_efficiency(payload: dict) -> Decimal | None:
    summary = payload.get("summary", {})
    if not isinstance(summary, dict):
        return None
    asleep = number(summary.get("minutesAsleep"))
    period = number(summary.get("minutesInSleepPeriod"))
    if asleep is None or period is None or period == 0:
        return None
    return (asleep / period) * 100


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
    value = first_number(payload, candidates)
    if value is None:
        for key, candidate in payload.items():
            if key not in ignored and not isinstance(candidate, (dict, list)):
                value = number(candidate)
                if value is not None:
                    break
    return value, payload.get("unit")
