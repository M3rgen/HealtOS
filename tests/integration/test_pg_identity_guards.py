"""Migration 20261021_0012 against PostgreSQL: guards, verify, upgrade/downgrade, locks."""

from __future__ import annotations

import asyncio
import uuid
from argparse import Namespace
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from app.google_health.data_types import REGISTRY
from app.models.identity import RawHealthDataIdentity, RawHealthDataSupersession
from app.models.state import RawHealthData
from app.parsers import normalize
from app.parsers.common import payload_hash, source_id, temporal
from app.services.raw_history import verify_guards
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


async def linked_pair(sessions) -> None:
    """A renamed duplicate linked to its successor, written as the backfill would."""
    old = await legacy(sessions, HEART, heart(60), T0, deleted=T0 + timedelta(days=1))
    new = await legacy(sessions, HEART, heart(60, name="Example Band"), T0 + timedelta(days=1))
    async with sessions.begin() as session:
        rows = {row.id: row for row in (await session.scalars(select(RawHealthData))).all()}
        session.add(
            RawHealthDataIdentity(
                data_type="heart-rate",
                identity_key="v2:20261018T090000:" + "a" * 64,
                raw_id=new,
                google_data_point_id=rows[new].google_data_point_id,
            )
        )
        session.add(
            RawHealthDataSupersession(
                old_raw_id=old,
                new_raw_id=new,
                data_type="heart-rate",
                old_google_data_point_id=rows[old].google_data_point_id,
                new_google_data_point_id=rows[new].google_data_point_id,
                reason="display-name-change",
            )
        )


async def test_identity_tables_refuse_update_delete_truncate(pg_sessions) -> None:
    await linked_pair(pg_sessions)
    for statement in (
        "UPDATE raw_health_data_identity SET identity_key = 'x'",
        "DELETE FROM raw_health_data_identity",
        "TRUNCATE raw_health_data_identity",
        "UPDATE raw_health_data_supersessions SET reason = 'duplicate'",
        "DELETE FROM raw_health_data_supersessions",
        "TRUNCATE raw_health_data_supersessions",
        "TRUNCATE raw_health_data CASCADE",
    ):
        with pytest.raises(DBAPIError, match="keeps every record"):
            await execute(pg_sessions, statement)
    # The guards also fire for replication-role sessions.
    with pytest.raises(DBAPIError, match="keeps every record"):
        await execute(
            pg_sessions,
            "SET LOCAL session_replication_role = replica",
            "DELETE FROM raw_health_data_supersessions",
        )


async def test_verify_detects_tampered_identity_guards(pg_sessions) -> None:
    async def guards() -> list[str]:
        async with pg_sessions() as session:
            return await verify_guards(session)

    assert await guards() == []
    table = "raw_health_data_supersessions"
    tampering = (
        (
            [f"ALTER TABLE {table} DISABLE TRIGGER {table}_append_only"],
            f"protective trigger {table}.{table}_append_only is disabled",
        ),
        (
            [
                f"DROP TRIGGER {table}_append_only ON {table}",
                f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW WHEN (false) EXECUTE FUNCTION healthos_refuse_change()",
                f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {table}_append_only",
            ],
            f"protective trigger {table}.{table}_append_only was redefined",
        ),
        (
            ["DROP TRIGGER raw_health_data_identity_no_truncate ON raw_health_data_identity"],
            "protective trigger missing: "
            "raw_health_data_identity.raw_health_data_identity_no_truncate",
        ),
    )
    for statements, message in tampering:
        await execute(pg_sessions, *statements)
        found = await guards()
        assert any(message in error for error in found), found


def _sync_url(database_url: str) -> str:
    return database_url.replace("+asyncpg", "+psycopg")


def test_migration_up_down_refuses_while_identity_rows_exist(
    database_url, alembic_config, monkeypatch
) -> None:
    from app.cli import main as cli

    command.upgrade(alembic_config, "20261020_0011")
    engine = create_engine(_sync_url(database_url))
    try:
        command.upgrade(alembic_config, "head")
        command.check(alembic_config)
        command.downgrade(alembic_config, "20261020_0011")  # empty tables: allowed
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT to_regclass('raw_health_data_identity')")) is None
            view = connection.scalar(text("SELECT pg_get_viewdef('analytics_steps')"))
        assert "supersessions" not in view
        command.upgrade(alembic_config, "head")

        async def fill() -> None:
            async_engine = create_async_engine(database_url, poolclass=NullPool)
            sessions = async_sessionmaker(async_engine, expire_on_commit=False)
            try:
                await linked_pair(sessions)
            finally:
                await async_engine.dispose()

        asyncio.run(fill())
        # verify (CLI) is green with the new guards in place.
        async_engine = create_async_engine(database_url, poolclass=NullPool)
        monkeypatch.setattr(
            cli, "SessionLocal", async_sessionmaker(async_engine, expire_on_commit=False)
        )
        result = CliRunner().invoke(cli.app, ["verify"])
        asyncio.run(async_engine.dispose())
        assert result.exit_code == 0, result.output

        with pytest.raises(RuntimeError, match="raw_health_data_identity holds"):
            command.downgrade(alembic_config, "20261020_0011")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20261021_0012"
            )
        alembic_config.cmd_opts = Namespace(x=["allow-identity-loss=1"])
        command.downgrade(alembic_config, "20261020_0011")
        alembic_config.cmd_opts = None
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20261020_0011"
            )
            # Raw rows, their versions and events were not touched by the downgrade.
            assert connection.scalar(text("SELECT count(*) FROM raw_health_data")) == 2
    finally:
        engine.dispose()


def test_0012_runs_next_to_a_dump_and_gives_up_on_a_writer(
    database_url, alembic_config, monkeypatch
) -> None:
    command.upgrade(alembic_config, "20261020_0011")
    monkeypatch.setenv("PGOPTIONS", "-c statement_timeout=20000")
    engine = create_engine(_sync_url(database_url))
    blocker = engine.connect()
    try:
        # What pg_dump holds on every table for the length of the dump: no conflict.
        tables = blocker.scalars(
            text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
        ).all()
        blocker.execute(text(f"LOCK TABLE {', '.join(tables)} IN ACCESS SHARE MODE"))
        command.upgrade(alembic_config, "head")
        blocker.rollback()
        command.downgrade(alembic_config, "20261020_0011")
        # What a writing transaction (a sync page) holds.
        blocker.execute(text("LOCK TABLE raw_health_data IN ROW EXCLUSIVE MODE"))
        alembic_config.cmd_opts = Namespace(x=["lock-timeout=1s"])
        with pytest.raises(RuntimeError, match="0012 could not lock its tables within 1s"):
            command.upgrade(alembic_config, "head")
        blocker.rollback()
        alembic_config.cmd_opts = None
        monkeypatch.delenv("PGOPTIONS")
        command.upgrade(alembic_config, "head")
    finally:
        blocker.close()
        engine.dispose()
