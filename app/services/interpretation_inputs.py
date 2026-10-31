"""Bounded, eligible source inputs and evidence-backed device continuity."""

from dataclasses import dataclass
from decimal import InvalidOperation

from sqlalchemy import String, cast, func, literal, select

from app.core.timeutil import as_utc
from app.models.eligibility import raw_counted
from app.models.manual import ManualMeasurement, ManualWorkout
from app.models.state import RawHealthData
from app.parsers.common import canonical_json, payload_hash
from app.parsers.common import number as loose_number
from app.parsers.identity import stable_source
from app.parsers.numeric import number
from app.services.finite_math import finite_scale
from app.services.health_windows import input_bounds
from app.services.interpretation_input_state import measurement_flags, source_decisions
from app.services.manual_flags import excluded_measurements

MAX_INPUT_RECORDS = 50_000
MAX_INPUT_BYTES = 16 * 1024 * 1024


def scaled_measurement(
    value: object, multiplier: float, divisor: float = 1
) -> tuple[float | None, list[str]]:
    """Validate a source number in its final unit, without an early float cast.

    The strict numeric parser separates absent/malformed fields from nonfinite
    numbers and exponents Decimal cannot represent. finite_scale bounds extreme
    exponents before exact arithmetic and distinguishes measured zero from a
    nonzero result too small for the response's float representation.
    Keep the caller's multiplication/division order for ordinary float rounding.
    This is a read-only conversion: normalized archive rows are never fallbacks.
    """
    numeric_reason = ["numeric_overflow:unit_conversion"]
    missing_reason = ["unsupported_or_missing_value"]
    try:
        source = number(value)
    except InvalidOperation:
        # NaN/Infinity is no measurement; only an exponent Decimal cannot hold overflows.
        loose = loose_number(value)
        nonfinite = loose is not None and not loose.is_finite()
        return None, missing_reason if nonfinite else numeric_reason
    if source is None:
        return None, missing_reason
    result = finite_scale(source, multiplier, divisor)
    return result, numeric_reason if result is None else []


def method_details(payload, metadata):
    source = metadata if isinstance(metadata, dict) else {}
    return {
        "measurementMethod": payload.get("measurementMethod"),
        "measurementLocation": payload.get("measurementLocation"),
        "recordingMethod": source.get("recordingMethod"),
    }


def method_key(payload, metadata):
    details = method_details(payload, metadata)
    return payload_hash(list(details.values()))


class BudgetError(Exception):
    pass


@dataclass
class Inputs:
    rows: list
    measurements: list
    workouts: list
    devices: dict
    fingerprint: str

    def source(self, kind, metadata, fallback):
        fingerprint = payload_hash(metadata)
        decision = self.devices.get((kind, fingerprint))
        # Device membership alone does not make recording methods equivalent.
        method = stable_source(metadata) or {}
        recording = method.get("recordingMethod") if isinstance(method, dict) else None
        return (
            "cihaz-" + payload_hash([kind, decision, recording])
            if decision is not None and isinstance(metadata, dict) and metadata
            else fallback
        )


async def load_inputs(session, first, last, timezone, kinds, *, manual=False, workouts=False):
    left, stop = input_bounds(first, last, timezone)
    window = (
        RawHealthData.data_type.in_(kinds),
        RawHealthData.source_start_time < stop,
        func.coalesce(RawHealthData.source_end_time, RawHealthData.source_start_time) >= left,
    )
    count, size = (
        await session.execute(
            select(func.count(), func.sum(func.length(cast(RawHealthData.raw_json, String)))).where(
                *window
            )
        )
    ).one()
    if count > MAX_INPUT_RECORDS or (size or 0) > MAX_INPUT_BYTES:
        raise BudgetError("input_budget_exceeded")
    found = list(
        await session.scalars(
            select(RawHealthData)
            .where(*window)
            .order_by(RawHealthData.id)
            .execution_options(populate_existing=True)
        )
    )
    if sum(len(canonical_json(r.raw_json).encode()) for r in found) > MAX_INPUT_BYTES:
        raise BudgetError("input_budget_exceeded")
    eligible = set(
        await session.scalars(
            select(RawHealthData.id).where(*window, raw_counted(data_types=tuple(kinds)))
        )
    )
    measurement_window = (
        literal(manual),
        ManualMeasurement.measured_at >= left,
        ManualMeasurement.measured_at < stop,
    )
    measurements = list(
        await session.scalars(
            select(ManualMeasurement)
            .where(*measurement_window)
            .order_by(ManualMeasurement.measured_at, ManualMeasurement.id)
            .limit(MAX_INPUT_RECORDS + 1)
            .execution_options(populate_existing=True)
        )
    )
    workouts = list(
        await session.scalars(
            select(ManualWorkout)
            .where(
                ManualWorkout.start_time < stop, ManualWorkout.end_time > left, literal(workouts)
            )
            .order_by(ManualWorkout.start_time, ManualWorkout.id)
            .limit(MAX_INPUT_RECORDS + 1)
            .execution_options(populate_existing=True)
        )
    )
    if len(measurements) + len(workouts) + count > MAX_INPUT_RECORDS:
        raise BudgetError("input_budget_exceeded")
    excluded = set(
        await session.scalars(
            select(ManualMeasurement.id).where(
                *measurement_window, ManualMeasurement.id.in_(excluded_measurements())
            )
        )
    )
    # Hash append-only events as well as effective state: mark then unmark also expires a cursor.
    flags = await measurement_flags(session, measurement_window, MAX_INPUT_RECORDS)
    if len(flags) > MAX_INPUT_RECORDS:
        raise BudgetError("input_budget_exceeded")
    keys = sorted({(r.data_type, payload_hash(r.raw_json.get("dataSource"))) for r in found})
    decisions = await source_decisions(session, keys)
    devices = {(r.data_type, r.fingerprint): r.device for r in decisions}
    signature = {
        "raw": [
            [
                str(r.id),
                r.raw_json,
                str(r.source_start_time),
                str(r.source_end_time),
                r.source_timezone,
                str(r.source_deleted_at),
                r.is_quarantined,
                r.id in eligible,
            ]
            for r in found
        ],
        "manual": [
            [str(r.id), r.metric, str(r.value), str(r.measured_at), r.id in excluded]
            for r in measurements
        ],
        "workouts": [
            [
                str(r.id),
                str(r.start_time),
                str(r.end_time),
                r.exercise_type,
                str(r.calories_kcal),
                str(r.distance_meters),
            ]
            for r in workouts
        ],
        "flags": [[str(key), action] for key, action in flags],
        "devices": [[r.data_type, r.fingerprint, r.device, r.id] for r in decisions],
    }
    return Inputs(
        [r for r in found if r.id in eligible],
        [r for r in measurements if r.id not in excluded],
        workouts,
        devices,
        payload_hash(signature),
    )


def evidence(raw, day, value=None, field=None, reasons=()):
    return dict(
        record=raw.google_data_point_id,
        date=str(day),
        value=value,
        start=as_utc(raw.source_start_time).isoformat() if raw.source_start_time else None,
        end=as_utc(raw.source_end_time).isoformat() if raw.source_end_time else None,
        source_timezone=raw.source_timezone,
        field=field,
        reason_codes=list(reasons),
    )


def fallback_source(raw):
    metadata = raw.raw_json.get("dataSource")
    return "kaynak-" + payload_hash(
        metadata
        if isinstance(metadata, dict) and metadata
        else {"record": raw.google_data_point_id}
    )
