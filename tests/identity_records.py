"""Synthetic records and helpers for the v2 identity tests (tests/test_identity_*.py).

The shapes follow what the archive holds: a Fitbit device known only by ``displayName``, an
Apple Health phone with a ``.<UUID>``-suffixed package, intervals with and without civil times.
No value here is real health data.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.google_health.data_types import REGISTRY
from app.models.state import RawHealthData
from app.parsers import normalize
from app.parsers.common import payload_hash, source_id, temporal
from app.services.storage import store_records
from app.sync.deletions import mark_source_deletions

HEART = REGISTRY["heart-rate"]
STEPS = REGISTRY["steps"]
SEDENTARY = REGISTRY["sedentary-period"]
SWIM = REGISTRY["swim-lengths-data"]
ZONES = REGISTRY["time-in-heart-rate-zone"]
OLD_NAME, NEW_NAME = "Example Band Pro", "Example Band"
PACKAGE = "com.example.health"
SUFFIXED = f"{PACKAGE}.0A1B2C3D-4E5F-6071-8293-A4B5C6D7E8F9"
T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)


def fitbit(name: str | None = OLD_NAME, method: str = "DERIVED") -> dict:
    source: dict = {"platform": "FITBIT", "recordingMethod": method}
    if name is not None:
        source["device"] = {"displayName": name}
    return source


def phone(package: str = SUFFIXED) -> dict:
    return {
        "platform": "HEALTH_KIT",
        "recordingMethod": "PASSIVELY_MEASURED",
        "device": {"formFactor": "PHONE", "manufacturer": "Example Inc."},
        "application": {"packageName": package},
    }


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def heart(bpm: int, at: datetime = T0, name: str = OLD_NAME, civil: bool = False) -> dict:
    sample: dict = {"physicalTime": iso(at)}
    if civil:
        sample |= {"civilTime": {"hours": 11}, "utcOffset": "7200s"}
    return {"dataSource": fitbit(name), "heartRate": {"sampleTime": sample, "beatsPerMinute": bpm}}


def interval(start: datetime, minutes: int, civil: bool = False) -> dict:
    value = {"startTime": iso(start), "endTime": iso(start + timedelta(minutes=minutes))}
    if civil:
        value |= {"civilStartTime": {"hours": 11}, "startUtcOffset": "7200s"}
        value |= {"civilEndTime": {"hours": 11}, "endUtcOffset": "7200s"}
    return value


def steps(count: int, start: datetime = T0, source: dict | None = None, civil: bool = False):
    return {
        "dataSource": source or phone(),
        "steps": {"interval": interval(start, 1, civil), "count": str(count)},
    }


def sedentary(minutes: int, start: datetime = T0) -> dict:
    return {
        "dataSource": fitbit(None),
        "sedentaryPeriod": {"interval": interval(start, minutes, civil=True)},
    }


def swim(strokes: int, civil: bool = False) -> dict:
    payload = {"interval": interval(T0, 1), "strokeCount": str(strokes)}
    if civil:
        payload["interval"] = interval(T0, 1, civil=True)
    return {"dataSource": fitbit(), "swimLengthsData": payload}


class Clock:
    def __init__(self) -> None:
        self.now = T0 + timedelta(hours=1)

    def advance(self, **delta: float) -> datetime:
        self.now += timedelta(**delta)
        return self.now


async def store(sessions, spec, *records):
    async with sessions.begin() as session:
        return await store_records(session, spec, list(records))


async def legacy(sessions, spec, record, seen: datetime, *, last=None, deleted=None):
    """A row as stored before v2: its v1 id, no alias; normalized like store_records did."""
    start, end, zone = temporal(spec, record)
    row = RawHealthData(
        id=uuid.uuid4(),
        google_data_point_id=source_id(spec, record),
        data_type=spec.name,
        source_start_time=start,
        source_end_time=end,
        source_timezone=zone,
        raw_json=record,
        payload_hash=payload_hash(record),
        first_seen_at=seen,
        last_seen_at=last or seen,
        source_deleted_at=deleted,
    )
    async with sessions.begin() as session:
        session.add(row)
        normalized = normalize(spec, record)
        if normalized is not None:
            session.add(normalized.model(**normalized.values))
    return row.id


async def raw_rows(sessions, spec) -> list[RawHealthData]:
    async with sessions() as session:
        query = select(RawHealthData).where(RawHealthData.data_type == spec.name)
        return list((await session.scalars(query.order_by(RawHealthData.first_seen_at))).all())


async def count(sessions, model) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def mark(sessions, spec, start, end, since) -> int:
    async with sessions.begin() as session:
        return await mark_source_deletions(session, spec, start, end, since)
