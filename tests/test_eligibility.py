"""One definition of what counts, shared by every reader (app/models/eligibility.py).

A record counts in the dashboard (UI, CSV, history), the workouts, Umay's tools (daily,
inventory, records) and the ``analytics_*`` views exactly when its raw row is live, neither the
raw nor the normalized row is quarantined, and it is not held by a counted row. Synthetic data,
in-memory SQLite: the view SQL runs on SQLite here, which does not show how PostgreSQL plans it.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from derived_records import steps
from identity_records import HEART, OLD_NAME, T0, fitbit, heart, store
from sqlalchemy import func, select, text, update

from app.api.dashboard import dashboard, export, metric_history, workouts
from app.models.eligibility import normalized_counted, raw_counted
from app.models.health import ExerciseSession, HeartRate, RestingHeartRate, Steps
from app.models.identity import RawHealthDataSupersession
from app.models.state import RawHealthData
from app.parsers.identity import source_fingerprint
from app.services import analytics_views, codex_health
from app.services.analytics_views import view_select
from app.services.dashboard import dashboard_data
from app.services.identity_backfill import identity_backfill
from app.services.identity_decisions import decide_sources

DAY = T0.date()
DELETED = T0 + timedelta(days=1)

# gid -> (raw row: None | "live" | "quarantined" | "deleted",
#         normalized row: None | "live" | "quarantined")
ROWS = {
    "live": ("live", "live"),
    "orphan": (None, "live"),
    "raw-quarantined": ("quarantined", "live"),
    "raw-deleted": ("deleted", "live"),
    "normalized-quarantined": ("live", "quarantined"),
    "raw-only": ("live", None),
    "held": ("live", "live"),
    "holder": ("live", "live"),
    "held-by-raw-quarantined": ("live", "live"),
    "holder-raw-quarantined": ("quarantined", "live"),
    "held-by-deleted": ("live", "live"),
    "holder-deleted": ("deleted", "live"),
    "held-not-in-force": ("live", "live"),
    "holder-not-in-force": ("live", "live"),
}
# (old, holder, fingerprints): one fingerprint is in force; two never joined are not.
LINKS = (
    ("held", "holder", ("fp", "fp")),
    ("held-by-raw-quarantined", "holder-raw-quarantined", ("fp", "fp")),
    ("held-by-deleted", "holder-deleted", ("fp", "fp")),
    ("held-not-in-force", "holder-not-in-force", ("fa", "fb")),
)
# What every reader counts. A lost holder lets the row it held count again (by design).
COUNTED = {
    "live",
    "raw-only",
    "holder",
    "held-by-raw-quarantined",
    "held-by-deleted",
    "held-not-in-force",
    "holder-not-in-force",
}
# Each normalized steps row its own power of ten: a day's total says which rows counted.
WEIGHTS = {gid: 10**index for index, gid in enumerate(gid for gid in ROWS if ROWS[gid][1])}


def decode(total) -> set[str]:
    if total is None or total == "":
        return set()
    total = int(total)
    found = {gid for gid, weight in WEIGHTS.items() if (total // weight) % 10 == 1}
    assert sum(WEIGHTS[gid] for gid in found) == total  # nothing counted twice
    return found


async def seed(sessions) -> None:
    raws = {}
    async with sessions.begin() as session:
        for index, (gid, (raw, normalized)) in enumerate(ROWS.items()):
            at = T0 + timedelta(minutes=index)
            if raw:
                raws[gid] = RawHealthData(
                    id=uuid.uuid4(),
                    data_type="steps",
                    google_data_point_id=gid,
                    raw_json={
                        "gid": gid,
                        **steps(gid, WEIGHTS.get(gid, 1), start=at, end=at + timedelta(minutes=1)),
                    },
                    payload_hash=f"h-{gid}",
                    source_start_time=at,
                    source_end_time=at + timedelta(minutes=1),
                    first_seen_at=at,
                    last_seen_at=at,
                    is_quarantined=raw == "quarantined",
                    source_deleted_at=DELETED if raw == "deleted" else None,
                )
                session.add(raws[gid])
            if normalized:
                session.add(
                    Steps(
                        google_data_point_id=gid,
                        start_time=at,
                        end_time=at + timedelta(minutes=1),
                        count=WEIGHTS[gid],
                        is_quarantined=normalized == "quarantined",
                    )
                )
        await session.flush()
        for old, new, (old_fp, new_fp) in LINKS:
            session.add(
                RawHealthDataSupersession(
                    old_raw_id=raws[old].id,
                    new_raw_id=raws[new].id,
                    data_type="steps",
                    old_google_data_point_id=old,
                    new_google_data_point_id=new,
                    reason="duplicate",
                    old_fingerprint=old_fp,
                    new_fingerprint=new_fp,
                    same_reading=True,
                    new_payload_hash=raws[new].payload_hash,
                )
            )


async def readers(sessions, monkeypatch) -> dict[str, set[str]]:
    """The steps records each reader counts on DAY."""
    monkeypatch.setattr(codex_health, "SessionLocal", sessions)
    found: dict[str, set[str]] = {}
    async with sessions() as session:
        for name, flags in (("dashboard_data", {}), ("strict", {"valid_sources_only": True})):
            data = await dashboard_data(session, DAY, 1, "UTC", **flags)
            found[name] = decode(data["days"][0]["steps"])
        found["/api/dashboard"] = decode(
            (await dashboard(1, DAY, "UTC", session))["days"][0]["steps"]
        )
        response = await export(1, DAY, "UTC", session)
        rows = list(csv.DictReader(io.StringIO(bytes(response.body).decode().lstrip("﻿"))))
        found["export.csv"] = decode(rows[0]["steps"])
        page = (datetime.now(UTC).date() - DAY).days // 30 + 1
        history = await metric_history("steps", page, "UTC", session)
        found["/api/dashboard/history"] = decode(
            next(row["value"] for row in history["days"] if row["date"] == DAY.isoformat())
        )
        view = await session.scalars(
            text(f"SELECT google_data_point_id FROM ({view_select('steps')}) AS v")
        )
        found["analytics_steps"] = set(view)
        found["normalized_counted"] = set(
            await session.scalars(
                select(Steps.google_data_point_id).where(normalized_counted(Steps))
            )
        )
        found["raw_counted"] = set(
            await session.scalars(
                select(RawHealthData.google_data_point_id).where(
                    RawHealthData.data_type == "steps", raw_counted()
                )
            )
        )
    day = {"start_date": DAY.isoformat(), "end_date": DAY.isoformat()}
    daily = await codex_health.read_health({"action": "daily", **day})
    found["codex daily"] = decode(daily["days"][0]["steps"])
    records = await codex_health.read_health({"action": "records", "data_type": "steps", **day})
    found["codex records"] = {row["payload"]["gid"] for row in records["records"]}
    inventory = await codex_health.read_health({"action": "inventory"})
    (stream,) = [row for row in inventory["streams"] if row["data_type"] == "steps"]
    assert stream["count"] == len(found["codex records"])
    return found


async def test_every_reader_counts_the_same_records(sessions, monkeypatch) -> None:
    """Parity: the normalized readers count exactly the counted records that have a
    normalized row, the raw readers exactly the counted raw rows."""
    await seed(sessions)
    found = await readers(sessions, monkeypatch)
    normalized = COUNTED - {"raw-only"}
    for name in ("codex records", "raw_counted"):
        assert found.pop(name) == COUNTED, name
    for name, gids in found.items():
        assert gids == normalized, name


@pytest.mark.parametrize(
    ("gid", "why"),
    [
        ("raw-quarantined", "a quarantine on the raw row alone hides the record"),
        ("normalized-quarantined", "a quarantine on the normalized row hides the raw row too"),
        ("orphan", "a normalized row without a raw row is not counted"),
        ("raw-deleted", "a row marked deleted at the source is not counted"),
        ("held", "a row held by a counted row is not counted"),
        ("holder-raw-quarantined", "a holder quarantined in its raw row alone is not counted"),
    ],
)
async def test_no_reader_counts_a_hidden_record(sessions, monkeypatch, gid, why) -> None:
    await seed(sessions)
    for name, gids in (await readers(sessions, monkeypatch)).items():
        assert gid not in gids, (name, why)


async def test_a_lost_holder_lets_the_row_it_held_count_again(sessions, monkeypatch) -> None:
    """By design: a link never hides a live record behind a row that is not counted, and a
    holder quarantined in its raw row alone is not counted beside it (no double count)."""
    await seed(sessions)
    for name, gids in (await readers(sessions, monkeypatch)).items():
        assert {"held-by-raw-quarantined", "held-by-deleted"} <= gids, name
        assert not {"holder-raw-quarantined", "holder-deleted"} & gids, name


async def test_valid_sources_only_changes_nothing(sessions) -> None:
    await seed(sessions)
    async with sessions() as session:
        default = await dashboard_data(session, DAY, 3, "UTC")
        strict = await dashboard_data(session, DAY, 3, "UTC", valid_sources_only=True)
    assert default == strict


async def test_source_deletions_included_still_hide_a_quarantine(sessions) -> None:
    """``include_source_deleted`` lets a row marked deleted in; a quarantine on either side
    (here only the normalized row's) still hides the daily summary."""
    days = {"quarantined": DAY - timedelta(days=1), "deleted": DAY}
    async with sessions.begin() as session:
        for gid, day in days.items():
            at = datetime.combine(day, datetime.min.time(), UTC)
            session.add(
                RawHealthData(
                    data_type="daily-resting-heart-rate",
                    google_data_point_id=gid,
                    raw_json={
                        "dailyRestingHeartRate": {"date": day.isoformat(), "beatsPerMinute": 60}
                    },
                    payload_hash=gid,
                    source_start_time=at,
                    first_seen_at=at,
                    last_seen_at=at,
                    source_deleted_at=DELETED if gid == "deleted" else None,
                )
            )
            session.add(
                RestingHeartRate(
                    google_data_point_id=gid,
                    timestamp=at,
                    bpm=60,
                    is_quarantined=gid == "quarantined",
                )
            )
    async with sessions() as session:
        default = await dashboard_data(session, DAY, 2, "UTC")
        with_deleted = await dashboard_data(session, DAY, 2, "UTC", include_source_deleted=True)
    assert [row["resting_bpm"] for row in default["days"]] == [None, None]
    assert [row["resting_bpm"] for row in with_deleted["days"]] == [60, None]


async def test_workouts_and_records_count_the_same_sessions(sessions, monkeypatch) -> None:
    states = {
        "ex-live": ("live", False),
        "ex-raw-quarantined": ("quarantined", False),
        "ex-normalized-quarantined": ("live", True),
        "ex-deleted": ("deleted", False),
        "ex-orphan": (None, False),
    }
    async with sessions.begin() as session:
        for index, (gid, (raw, quarantined)) in enumerate(states.items()):
            at = T0 + timedelta(minutes=10 * index)
            if raw:
                session.add(
                    RawHealthData(
                        data_type="exercise",
                        google_data_point_id=gid,
                        raw_json={
                            "gid": gid,
                            "exercise": {
                                "interval": {
                                    "startTime": at.isoformat(),
                                    "endTime": (at + timedelta(minutes=5)).isoformat(),
                                },
                                "exerciseType": gid,
                            },
                        },
                        payload_hash=gid,
                        source_start_time=at,
                        source_end_time=at + timedelta(minutes=5),
                        first_seen_at=at,
                        last_seen_at=at,
                        is_quarantined=raw == "quarantined",
                        source_deleted_at=DELETED if raw == "deleted" else None,
                    )
                )
            session.add(
                ExerciseSession(
                    google_data_point_id=gid,
                    exercise_type=gid,
                    start_time=at,
                    end_time=at + timedelta(minutes=5),
                    is_quarantined=quarantined,
                )
            )
    monkeypatch.setattr(codex_health, "SessionLocal", sessions)
    async with sessions() as session:
        shown = await workouts(DAY.replace(day=1), "UTC", session)
    day = {"start_date": DAY.isoformat(), "end_date": DAY.isoformat()}
    records = await codex_health.read_health({"action": "records", "data_type": "exercise", **day})
    assert [row["exercise_type"] for row in shown] == ["ex-live"]
    assert [row["payload"]["gid"] for row in records["records"]] == ["ex-live"]


# --- real write and decision paths (identity-backfill, identity-decide) ---------------------

CHARGE = "Example Charge"
OLD_FP, CHARGE_FP = source_fingerprint(fitbit(OLD_NAME)), source_fingerprint(fitbit(CHARGE))


async def two_bands(sessions) -> None:
    """Four minutes of heart rate from two bands with equal readings (61.5 bpm on average)."""
    for minute in range(4):
        at = T0 + timedelta(minutes=minute)
        await store(
            sessions, HEART, heart(60 + minute, at=at), heart(60 + minute, at=at, name=CHARGE)
        )
    await identity_backfill(sessions)


async def heart_counts(sessions, monkeypatch) -> dict[str, object]:
    monkeypatch.setattr(codex_health, "SessionLocal", sessions)
    async with sessions() as session:
        raw = await session.scalar(
            select(func.count())
            .select_from(RawHealthData)
            .where(RawHealthData.data_type == "heart-rate", raw_counted())
        )
        normalized = await session.scalar(
            select(func.count()).select_from(HeartRate).where(normalized_counted(HeartRate))
        )
        view = len((await session.execute(text(view_select("heart_rate")))).all())
        data = await dashboard_data(session, DAY, 1, "UTC")
    inventory = await codex_health.read_health({"action": "inventory"})
    (stream,) = [row for row in inventory["streams"] if row["data_type"] == "heart-rate"]
    return {
        "raw": raw,
        "normalized": normalized,
        "view": view,
        "inventory": stream["count"],
        "hour": data["charts"]["heart_rate"][T0.hour],
    }


def same(count: int) -> dict[str, object]:
    return {"raw": count, "normalized": count, "view": count, "inventory": count, "hour": 61.5}


async def test_a_rejected_link_counts_both_bands_again(sessions, clock, monkeypatch) -> None:
    await two_bands(sessions)
    assert await heart_counts(sessions, monkeypatch) == same(8)
    await decide_sources(sessions, "heart-rate", "link", OLD_FP[:12], CHARGE_FP[:12])
    assert await heart_counts(sessions, monkeypatch) == same(4)
    await decide_sources(sessions, "heart-rate", "reject", OLD_FP[:12], CHARGE_FP[:12])
    assert await heart_counts(sessions, monkeypatch) == same(8)
    async with sessions() as session:  # the links stay, as history
        assert (
            await session.scalar(select(func.count()).select_from(RawHealthDataSupersession)) == 4
        )


@pytest.mark.parametrize("loss", ["quarantined", "deleted"])
async def test_a_lost_holder_is_never_counted_twice(sessions, clock, monkeypatch, loss) -> None:
    """The holder's raw row alone is quarantined (or marked deleted): the row it held
    counts again, and every reader still counts four readings, averaging 61.5."""
    await two_bands(sessions)
    await decide_sources(sessions, "heart-rate", "link", OLD_FP[:12], CHARGE_FP[:12])
    async with sessions() as session:
        link = (await session.scalars(select(RawHealthDataSupersession).limit(1))).one()
    change = {"is_quarantined": True} if loss == "quarantined" else {"source_deleted_at": DELETED}
    async with sessions.begin() as session:
        await session.execute(
            update(RawHealthData).where(RawHealthData.id == link.new_raw_id).values(**change)
        )
    assert await heart_counts(sessions, monkeypatch) == same(4)
    async with sessions() as session:
        assert await session.scalar(
            select(func.count())
            .select_from(RawHealthData)
            .where(RawHealthData.id == link.old_raw_id, raw_counted())
        )


# --- the views of migration 20261024_0013 ----------------------------------------------------


def migration(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, Path("migrations/versions") / path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_model_and_migration_0013_views_are_the_same() -> None:
    current = migration("migration_0013", "20261024_0013_counted_views.py")
    previous = migration("migration_0012", "20261021_0012_record_identity.py")
    assert (current.revision, current.down_revision) == ("20261024_0013", previous.revision)
    assert current.NORMALIZED_TABLES == analytics_views.NORMALIZED_TABLES
    for table in analytics_views.NORMALIZED_TABLES:
        # The views verify compares with are the ones the migration creates ...
        assert current._view(table) == (
            f"CREATE OR REPLACE VIEW analytics_{table} AS {view_select(table)}"
        )
        # ... and downgrade() restores the ones of 0012.
        assert current._view_0012(table) == previous._view(table, current_only=True)
        assert "DROP" not in current._view(table) + current._view_0012(table)


def test_migration_0013_drops_nothing_on_upgrade() -> None:
    source = Path("migrations/versions/20261024_0013_counted_views.py").read_text("utf-8")
    assert "DROP" not in source.upper()  # views are replaced, in either direction
