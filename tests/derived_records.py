"""Synthetic payloads for the source-separated daily adapters and their write paths."""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from app.google_health.data_types import REGISTRY
from app.models.derived import DerivedDay
from app.models.manual import ManualMeasurement
from app.services.canonical_daily import METHOD
from app.services.derived import derived_range
from app.services.storage import store_records

DAY = date(2026, 9, 29)
T0 = datetime(2026, 9, 29, tzinfo=UTC)
BAND = {"platform": "FITBIT", "device": {"displayName": "Synthetic band"}}
PHONE = {"platform": "HEALTH_KIT", "device": {"formFactor": "PHONE"}}

ADAPTER_KINDS = (
    "steps",
    "sleep",
    "daily-resting-heart-rate",
    "daily-heart-rate-variability",
    "daily-oxygen-saturation",
)
RAW_MUTATIONS = (
    "insert",
    "move",
    "delete",
    "revive",
    "supersession",
    "device",
    "unchanged",
    "payload",
    "zone",
    "quarantine",
    "end",
)


def point(kind, name, payload, source=BAND):
    return {
        "name": f"synthetic/{kind}/{name}",
        "dataSource": source,
        REGISTRY[kind].json_key: payload,
    }


def summary(kind, name="one", day=DAY, source=BAND, **values):
    return point(kind, name, {"date": day.isoformat(), **values}, source)


def steps(name="one", count=120, start=T0, end=None, source=BAND):
    return point(
        "steps",
        name,
        {
            "count": count,
            "interval": {
                "startTime": start.isoformat(),
                "endTime": (end or start + timedelta(hours=1)).isoformat(),
            },
        },
        source,
    )


def sleep(name="one", start=None, end=None, source=BAND, **payload):
    start, end = start or T0 + timedelta(hours=1), end or T0 + timedelta(hours=3)
    return point(
        "sleep",
        name,
        {"interval": {"startTime": start.isoformat(), "endTime": end.isoformat()}, **payload},
        source,
    )


def stage(kind, left, right):
    return {
        "type": kind,
        "startTime": (T0 + timedelta(hours=left)).isoformat(),
        "endTime": (T0 + timedelta(hours=right)).isoformat(),
    }


async def store(sessions, kind, *records):
    async with sessions() as session, session.begin():
        return await store_records(session, REGISTRY[kind], list(records))


async def read(sessions, first=DAY, last=None, timezone="UTC"):
    async with sessions() as session, session.begin():
        return await derived_range(session, first, last or first, timezone)


def streams(card, metric=None, day=0):
    rows = card["days"][day]["result"]["streams"]
    return rows if metric is None else [row for row in rows if row["metric"] == metric]


async def cache(sessions, day=DAY, timezone="UTC"):
    async with sessions() as session:
        return await session.get(DerivedDay, (day, timezone, METHOD))


async def manual(sessions, value, at=T0, metric="weight"):
    async with sessions() as session, session.begin():
        row = ManualMeasurement(metric=metric, value=value, measured_at=at)
        session.add(row)
        await session.flush()
        return row.id


async def snapshot(session):
    """All columns, including archive history and manual events, before and after builds."""
    from app.db.base import Base

    return {
        name: sorted(repr(tuple(row)) for row in (await session.execute(select(table))).all())
        for name, table in Base.metadata.tables.items()
        if name != "derived_days"
    }


async def fail_derived_range(sessions, monkeypatch, first=DAY, last=DAY, timezone="UTC"):
    from app.services import derived

    async def broken(*args):
        raise ValueError("synthetic-private-value 998877")

    with monkeypatch.context() as patch:
        patch.setattr(derived, "canonical_day", broken)
        await derived.build_range(sessions, first, last, timezone)


async def raw_mutation(sessions, kind, operation):
    """Isolate a raw/identity trigger from normalized-table writes; synthetic archive only."""
    from sqlalchemy import insert, update

    from app.models.identity import RawHealthDataSupersession, RawSourceDevice
    from app.models.state import RawHealthData
    from app.parsers.common import payload_hash

    def values(name, offset=0):
        return dict(
            data_type=kind,
            google_data_point_id=f"synthetic/{kind}/{name}",
            raw_json={"synthetic": name},
            payload_hash=payload_hash({"synthetic": name}),
            source_start_time=T0 + timedelta(days=offset),
            source_end_time=T0 + timedelta(days=offset, hours=1),
            first_seen_at=T0,
            last_seen_at=T0,
        )

    async with sessions.begin() as session:
        old, new = RawHealthData(**values("old")), RawHealthData(**values("new", 1))
        session.add_all([old, new])
        await session.flush()
        link = dict(
            old_raw_id=old.id,
            new_raw_id=new.id,
            data_type=kind,
            old_google_data_point_id=old.google_data_point_id,
            new_google_data_point_id=new.google_data_point_id,
            reason="duplicate",
            old_fingerprint="a" * 64,
            new_fingerprint="a" * 64,
            same_reading=True,
            new_payload_hash=new.payload_hash,
        )
        if operation == "device":
            session.add(RawHealthDataSupersession(**link))
        if operation == "revive":
            old.source_deleted_at = T0
    if operation == "insert":
        return insert(RawHealthData).values(**values("late"))
    if operation == "supersession":
        return insert(RawHealthDataSupersession).values(**link)
    if operation == "device":
        return insert(RawSourceDevice).values(
            data_type=kind, fingerprint="a" * 64, device="b" * 64, decided_at=T0
        )
    changes = {
        "move": dict(
            source_start_time=T0 + timedelta(days=1),
            source_end_time=T0 + timedelta(days=1, hours=1),
        ),
        "delete": dict(source_deleted_at=T0),
        "revive": dict(source_deleted_at=None),
        "unchanged": dict(raw_json=RawHealthData.raw_json, last_seen_at=T0 + timedelta(hours=2)),
        "payload": dict(raw_json={"synthetic": "corrected"}),
        "zone": dict(source_timezone="UTC"),
        "quarantine": dict(is_quarantined=True),
        "end": dict(source_end_time=T0 + timedelta(hours=2)),
    }
    return update(RawHealthData).where(RawHealthData.id == old.id).values(**changes[operation])
