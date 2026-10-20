"""Stable v2 identities against PostgreSQL: store_records and deletion marking.

Synthetic records only: a device rename, a growing interval, a genuine deletion and two devices
recording at the same instant.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.google_health.data_types import REGISTRY
from app.models.identity import RawHealthDataIdentity
from app.models.raw_versions import RawHealthDataVersion
from app.models.state import RawHealthData
from app.parsers import normalize
from app.parsers.common import payload_hash, source_id, temporal
from app.services.storage import store_records
from app.sync.deletions import mark_source_deletions

pytestmark = pytest.mark.integration

HEART = REGISTRY["heart-rate"]
STEPS = REGISTRY["steps"]
SEDENTARY = REGISTRY["sedentary-period"]
T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
SUFFIXED = "com.example.health.0A1B2C3D-4E5F-6071-8293-A4B5C6D7E8F9"


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def heart(bpm: int, at: datetime = T0, name: str = "Example Band Pro") -> dict:
    return {
        "dataSource": {
            "platform": "FITBIT",
            "recordingMethod": "DERIVED",
            "device": {"displayName": name},
        },
        "heartRate": {"sampleTime": {"physicalTime": iso(at)}, "beatsPerMinute": bpm},
    }


def steps(count: int, package: str = SUFFIXED, start: datetime = T0) -> dict:
    return {
        "dataSource": {
            "platform": "HEALTH_KIT",
            "recordingMethod": "PASSIVELY_MEASURED",
            "device": {"formFactor": "PHONE", "manufacturer": "Example Inc."},
            "application": {"packageName": package},
        },
        "steps": {
            "interval": {"startTime": iso(start), "endTime": iso(start + timedelta(minutes=1))},
            "count": str(count),
        },
    }


def sedentary(minutes: int) -> dict:
    return {
        "dataSource": {"platform": "FITBIT", "recordingMethod": "DERIVED"},
        "sedentaryPeriod": {
            "interval": {"startTime": iso(T0), "endTime": iso(T0 + timedelta(minutes=minutes))}
        },
    }


async def store(sessions, spec, *records):
    async with sessions.begin() as session:
        return await store_records(session, spec, list(records))


async def legacy(sessions, spec, record, seen, *, deleted=None) -> uuid.UUID:
    start, end, zone = temporal(spec, record)
    raw_id = uuid.uuid4()
    async with sessions.begin() as session:
        session.add(
            RawHealthData(
                id=raw_id,
                google_data_point_id=source_id(spec, record),
                data_type=spec.name,
                source_start_time=start,
                source_end_time=end,
                source_timezone=zone,
                raw_json=record,
                payload_hash=payload_hash(record),
                first_seen_at=seen,
                last_seen_at=seen,
                source_deleted_at=deleted,
            )
        )
        normalized = normalize(spec, record)
        if normalized is not None:
            session.add(normalized.model(**normalized.values))
    return raw_id


async def scalar(sessions, query):
    async with sessions() as session:
        return await session.scalar(query)


async def execute(sessions, *statements: str) -> None:
    async with sessions.begin() as session:
        for statement in statements:
            await session.execute(text(statement))


async def test_rename_keeps_one_row_a_version_and_no_deletion_mark(pg_sessions, clock) -> None:
    await legacy(pg_sessions, HEART, heart(60), T0)
    await store(pg_sessions, HEART, heart(61, at=T0 + timedelta(minutes=1)))
    clock.now = T0 + timedelta(hours=7)
    fetched_since = clock.now
    renamed = [heart(60, name="Example Band"), heart(61, T0 + timedelta(minutes=1), "Example Band")]
    stats = await store(pg_sessions, HEART, *renamed)
    assert (stats.inserted, stats.updated, stats.versions_kept) == (0, 2, 2)
    async with pg_sessions.begin() as session:
        marked = await mark_source_deletions(
            session, HEART, T0, T0 + timedelta(hours=1), fetched_since
        )
    assert marked == 0
    assert await scalar(pg_sessions, select(func.count()).select_from(RawHealthData)) == 2
    assert await scalar(pg_sessions, select(func.count()).select_from(RawHealthDataVersion)) == 2
    assert await scalar(pg_sessions, select(func.count()).select_from(RawHealthDataIdentity)) == 2


async def test_growing_interval_and_genuine_deletion(pg_sessions, clock) -> None:
    for minutes in (15, 30):
        await store(pg_sessions, SEDENTARY, sedentary(minutes))
        clock.now += timedelta(minutes=15)
    assert await scalar(pg_sessions, select(func.count()).select_from(RawHealthData)) == 1
    kept, gone = heart(60), heart(61, at=T0 + timedelta(minutes=2))
    await store(pg_sessions, HEART, kept, gone)
    clock.now += timedelta(hours=1)
    fetched_since = clock.now
    await store(pg_sessions, HEART, kept)
    async with pg_sessions.begin() as session:
        marked = await mark_source_deletions(
            session, HEART, T0, T0 + timedelta(hours=1), fetched_since
        )
    assert marked == 1


async def test_two_devices_at_one_instant_are_not_merged(pg_sessions, clock) -> None:
    band, watch = heart(60), heart(64, name="Example Watch")
    await store(pg_sessions, HEART, band, watch)
    clock.now += timedelta(hours=1)
    await store(pg_sessions, HEART, watch, band)
    assert await scalar(pg_sessions, select(func.count()).select_from(RawHealthData)) == 2
    assert await scalar(pg_sessions, select(func.count()).select_from(RawHealthDataVersion)) == 0
