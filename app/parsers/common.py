from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.google_health.data_types import DataTypeSpec


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def payload_for(spec: DataTypeSpec, record: dict) -> dict:
    value = record.get(spec.json_key, record.get(spec.payload_key, record.get(spec.name, {})))
    return value if isinstance(value, dict) else {}


def source_id(spec: DataTypeSpec, record: object) -> str:
    if isinstance(record, dict):
        for key in ("name", "dataPointName"):
            if isinstance(record.get(key), str) and record[key]:
                return record[key]
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
            return start, end, str(timezone) if timezone is not None else None
    sample = payload.get("sampleTime", {})
    if isinstance(sample, dict):
        timestamp = parse_datetime(sample.get("physicalTime"))
        timezone = sample.get("timeZone") or sample.get("utcOffset")
        if timestamp:
            return timestamp, timestamp, str(timezone) if timezone is not None else None
    day = parse_date(payload.get("date"))
    if day:
        timestamp = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        return timestamp, timestamp, payload.get("timeZone")
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
    except ValueError:
        return None


def parse_date(value: object) -> date | None:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, dict):
        try:
            return date(int(value["year"]), int(value["month"]), int(value["day"]))
        except (KeyError, TypeError, ValueError):
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
    return device.get("displayName") or device.get("model")


def first_number(payload: dict, candidates: tuple[str, ...]) -> Decimal | None:
    for name in candidates:
        value = number(payload.get(name))
        if value is not None:
            return value
    return None
