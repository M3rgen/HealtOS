from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.google_health.data_types import DataTypeSpec
from app.parsers.text import optional_text


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def payload_for(spec: DataTypeSpec, record: dict) -> dict:
    value = record.get(spec.json_key, record.get(spec.payload_key, record.get(spec.name, {})))
    return value if isinstance(value, dict) else {}


def google_name(record: object) -> str | None:
    """The record's own Google id, when it has one."""
    if isinstance(record, dict):
        for key in ("name", "dataPointName"):
            if isinstance(record.get(key), str) and record[key]:
                return record[key]
    return None


def source_id(spec: DataTypeSpec, record: object) -> str:
    """The record's Google id, or its v1 synthetic id (see app/parsers/identity.py for v2).

    v1 is frozen: rows stored under it keep it forever, and a record without a v2 alias is
    looked up by it first, so changing this function would re-archive every such record.
    """
    name = google_name(record)
    if name is not None:
        return name
    identity: dict[str, Any] = {"dataType": spec.name}
    if isinstance(record, dict):
        payload = payload_for(spec, record)
        for key in ("interval", "sampleTime", "date"):
            if key in payload:
                identity[key] = payload[key]
        for key in (
            "heartRateZone",
            "activityLevelType",
            "measurementLocation",
            "measurementMethod",
            "swimStrokeType",
        ):
            if key in payload:
                identity[key] = payload[key]
        identity["dataSource"] = record.get("dataSource")
        if "startTime" in record:
            identity["startTime"] = record.get("startTime")
            identity["endTime"] = record.get("endTime")
    else:
        identity["record"] = record
    return f"synthetic:{payload_hash(identity)}"


def temporal(
    spec: DataTypeSpec, record: dict
) -> tuple[datetime | None, datetime | None, str | None]:
    payload = payload_for(spec, record)
    interval = payload.get("interval", {})
    if isinstance(interval, dict):
        start = parse_datetime(interval.get("startTime"))
        end = parse_datetime(interval.get("endTime"))
        timezone = interval.get("startTimeZone") or interval.get("startUtcOffset")
        if start or end:
            return start, end, optional_text(timezone)
    sample = payload.get("sampleTime", {})
    if isinstance(sample, dict):
        timestamp = parse_datetime(sample.get("physicalTime"))
        timezone = sample.get("timeZone") or sample.get("utcOffset")
        if timestamp:
            return timestamp, timestamp, optional_text(timezone)
    day = parse_date(payload.get("date"))
    if day:
        timestamp = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        return timestamp, timestamp, optional_text(payload.get("timeZone"))
    start = parse_datetime(record.get("startTime"))
    end = parse_datetime(record.get("endTime"))
    if start or end:
        return start, end, None
    return None, None, None


def parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    except (ValueError, OverflowError):
        # A valid local datetime can overflow Python's year range when converted to UTC.
        return None


def _date_component(value: object) -> int | None:
    """Accept only lossless integer components from a Google JSON date.

    Booleans are JSON flags even though Python treats them as integers. Finite,
    whole floats are valid; fractional and nonfinite floats must never be truncated.
    Strings retain int()'s existing integer syntax and conversion limits.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # is_integer() is false for NaN and both infinities as well as fractions.
        if not value.is_integer():
            return None
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    # Lists, objects and null are not numeric date components.
    return None


def parse_date(value: object) -> date | None:
    """Parse an ISO date or Google's year/month/day object without inventing a date.

    Components are validated first (no flag or fraction becomes a day); calendar
    construction guards impossible dates and integer overflow.
    """
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, dict):
        try:
            year, month, day = (_date_component(value[key]) for key in ("year", "month", "day"))
            if year is None or month is None or day is None:
                return None
            return date(year, month, day)
        except (KeyError, ValueError, OverflowError):
            return None
    return None


def number(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def duration_seconds(value: object) -> Decimal | None:
    if isinstance(value, str) and value.endswith("s"):
        return number(value[:-1])
    return number(value)


def device_name(record: dict) -> str | None:
    source = record.get("dataSource", {})
    device = source.get("device", {}) if isinstance(source, dict) else {}
    if not isinstance(device, dict):
        return None
    return optional_text(device.get("displayName") or device.get("model"))


def first_number(payload: dict, candidates: tuple[str, ...]) -> Decimal | None:
    for name in candidates:
        value = number(payload.get(name))
        if value is not None:
            return value
    return None
