"""``health-mirror identity-backfill`` against PostgreSQL, and readers skipping its links."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.google_health.data_types import REGISTRY
from app.models.identity import RawHealthDataIdentity, RawHealthDataSupersession
from app.models.raw_versions import RawHealthDataEvent
from app.models.state import RawHealthData
from app.parsers import normalize
from app.parsers.common import payload_hash, source_id, temporal
from app.services.dashboard import dashboard_data
from app.services.identity_backfill import identity_backfill
from app.services.storage import store_records

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


async def archive(pg_sessions) -> dict[str, uuid.UUID]:
    day = timedelta(days=1)
    return {
        "old": await legacy(pg_sessions, HEART, heart(60), T0),
        "new": await legacy(pg_sessions, HEART, heart(60, name="Example Band"), T0 + day),
        "deleted": await legacy(
            pg_sessions, HEART, heart(70, at=T0 + timedelta(minutes=4)), T0, deleted=T0 + day
        ),
        "suffixed": await legacy(pg_sessions, STEPS, steps(40), T0),
        "plain": await legacy(pg_sessions, STEPS, steps(40, "com.example.health"), T0 + day),
    }


async def test_backfill_dry_run_idempotent_and_resumable(pg_sessions) -> None:
    ids = await archive(pg_sessions)
    dry = await identity_backfill(pg_sessions, dry_run=True)
    assert await scalar(pg_sessions, select(func.count()).select_from(RawHealthDataIdentity)) == 0
    assert dry["heart-rate"].linked == {"display-name-change": 1}
    # Stopped after heart-rate (a run of one type), then resumed for the rest.
    await identity_backfill(pg_sessions, data_types=["heart-rate"], batch_size=1)
    await identity_backfill(pg_sessions, data_types=["steps"], after=T0)
    async with pg_sessions() as session:
        found = {
            row.old_raw_id: (row.new_raw_id, row.reason)
            for row in (await session.scalars(select(RawHealthDataSupersession))).all()
        }
        merged = await session.scalar(
            select(func.count()).where(RawHealthDataEvent.event == "identity-merged")
        )
        deleted = await session.get(RawHealthData, ids["deleted"])
    assert found == {
        ids["old"]: (ids["new"], "display-name-change"),
        ids["suffixed"]: (ids["plain"], "package-suffix-change"),
    }
    assert merged == 2 and deleted is not None and deleted.source_deleted_at is not None
    again = await identity_backfill(pg_sessions, batch_size=2)
    assert sum(sum(s.linked.values()) + s.aliases_added for s in again.values()) == 0
    assert again["heart-rate"].already_superseded == 1


async def test_dashboard_and_analytics_views_skip_superseded_rows(pg_sessions) -> None:
    await archive(pg_sessions)

    async def counted() -> tuple:
        async with pg_sessions() as session:
            data = await dashboard_data(session, date(2026, 10, 18), 1, "UTC")
            view = await session.scalar(text("SELECT sum(count) FROM analytics_steps"))
        return data["days"][0]["steps"], float(view)

    assert await counted() == (80, 80.0)
    await identity_backfill(pg_sessions)
    assert await counted() == (40, 40.0)
