"""Migration 20261024_0014 against PostgreSQL: the flag events' guards, verify, readers."""

from __future__ import annotations

import asyncio
import uuid
from argparse import Namespace
from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from app.services.dashboard import dashboard_data
from app.services.manual_flags import excluded_measurement_ids, record_flag
from app.services.raw_history import verify_guards

pytestmark = pytest.mark.integration

TABLE = "manual_measurement_flag_events"


async def execute(sessions, *statements: str) -> None:
    async with sessions.begin() as session:
        for statement in statements:
            await session.execute(text(statement))


async def measurement(sessions, value: float, at: str) -> uuid.UUID:
    measurement_id = uuid.uuid4()
    await execute(
        sessions,
        "INSERT INTO manual_measurements (id, metric, value, measured_at) "
        f"VALUES ('{measurement_id}', 'weight', {value}, '{at}')",
    )
    return measurement_id


async def test_flag_events_refuse_update_delete_truncate(pg_sessions) -> None:
    flagged = await measurement(pg_sessions, 70.5, "2026-09-28T07:30:00Z")
    async with pg_sessions.begin() as session:
        assert await record_flag(session, flagged, "test", True)
    for statement in (
        f"UPDATE {TABLE} SET action = 'unmark'",
        f"DELETE FROM {TABLE}",
        f"TRUNCATE {TABLE}",
        # No foreign key: the measurements' own TRUNCATE refusal still answers.
        "TRUNCATE manual_measurements",
        "UPDATE manual_measurements SET value = 99",
    ):
        with pytest.raises(DBAPIError, match="keeps every record"):
            await execute(pg_sessions, statement)
    with pytest.raises(DBAPIError, match="keeps every record"):
        await execute(
            pg_sessions, "SET LOCAL session_replication_role = replica", f"DELETE FROM {TABLE}"
        )
    async with pg_sessions() as session:
        assert await excluded_measurement_ids(session) == {flagged}


async def test_verify_detects_tampered_flag_guards(pg_sessions) -> None:
    async def guards() -> list[str]:
        async with pg_sessions() as session:
            return await verify_guards(session)

    assert await guards() == []
    await execute(pg_sessions, f"ALTER TABLE {TABLE} DISABLE TRIGGER {TABLE}_append_only")
    assert any(
        f"protective trigger {TABLE}.{TABLE}_append_only is disabled" in error
        for error in await guards()
    )


async def test_the_profile_falls_back_past_a_test_entry(pg_sessions) -> None:
    await measurement(pg_sessions, 80.0, "2026-09-20T07:30:00Z")
    flagged = await measurement(pg_sessions, 99.0, "2026-09-28T07:30:00Z")
    async with pg_sessions.begin() as session:
        await record_flag(session, flagged, "test", True)
    async with pg_sessions() as session:
        data = await dashboard_data(session, datetime(2026, 9, 29, tzinfo=UTC).date(), 7, "UTC")
    assert data["profile"]["weight"]["value"] == 80.0


def test_downgrade_refuses_while_flags_exist(database_url, alembic_config) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url.replace("+asyncpg", "+psycopg"))
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {TABLE} (measurement_id, flag, action) "
                    "VALUES (gen_random_uuid(), 'test', 'mark')"
                )
            )
        with pytest.raises(RuntimeError, match=f"{TABLE} holds 1 row"):
            command.downgrade(alembic_config, "20261024_0013")
        alembic_config.cmd_opts = Namespace(x=["allow-flag-loss=1"])
        command.downgrade(alembic_config, "20261024_0013")
        alembic_config.cmd_opts = None
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20261024_0013"
            )
            assert connection.scalar(text("SELECT count(*) FROM manual_measurements")) == 0
        command.upgrade(alembic_config, "head")
    finally:
        engine.dispose()


def test_verify_on_a_database_still_at_0013_says_so(database_url, alembic_config, monkeypatch):
    """Without the gate, verify's guard check (a LIKE copy of every guarded table) failed on
    SQL against a database not upgraded to 0014 yet; it names the revision instead."""
    from app.cli import main as cli

    command.upgrade(alembic_config, "20261024_0013")
    engine = create_async_engine(database_url, poolclass=NullPool)
    monkeypatch.setattr(cli, "SessionLocal", async_sessionmaker(engine, expire_on_commit=False))
    try:
        result = CliRunner().invoke(cli.app, ["verify"])
    finally:
        asyncio.run(engine.dispose())
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "the database is at revision 20261024_0013" in result.output
    assert "20261024_0014" in result.output
    assert "alembic upgrade" in result.output
