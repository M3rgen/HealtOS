"""Record identity, third review: the findings that need PostgreSQL (synthetic records only).

Lock order between deletion marking and a page (no pair inserts in between), a reconciler
batch that meets another transaction's row lock, the downgrade guard for the sightings, a
verify against a database still at 0011, the index behind the analytics views, and the
dashboard total after a blip on the old form (B1).
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, timedelta

import pytest
from alembic import command
from identity_records import (
    HEART,
    NEW_NAME,
    PACKAGE,
    STEPS,
    T0,
    heart,
    legacy,
    mark,
    phone,
    steps,
    store,
)
from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from app.services import identity_backfill as reconciler
from app.services.dashboard import dashboard_data
from app.services.identity import verify_identities
from app.services.identity_backfill import identity_backfill

pytestmark = pytest.mark.integration

HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)


async def test_deletion_marks_never_wait_for_a_writer_and_insert_nothing(pg_sessions, clock):
    """Deadlock window: marking inserted raw_source_pairs between chunks of row locks while a
    page inserted them before its row locks. Marking inserts nothing, and (review 4: it marks
    chunk by chunk, so a long window is never held in memory) it locks only rows nobody else
    holds (SKIP LOCKED): it never waits for a writer, so no circle can form."""
    from app.sync import deletions

    for minute in range(6):
        at = T0 + minute * MINUTE
        await store(pg_sessions, HEART, heart(60 + minute, at=at), heart(90, at=at, name="W"))
    statements: list[str] = []
    engine = pg_sessions.kw["bind"].sync_engine

    def seen(_connection, _cursor, statement, *_args) -> None:
        statements.append(" ".join(statement.split()))

    deletions_chunk = deletions.CHUNK
    deletions.CHUNK = 4
    event.listen(engine, "before_cursor_execute", seen)
    try:
        fetched_since = clock.advance(hours=1)
        assert await mark(pg_sessions, HEART, T0 - HOUR, T0 + HOUR, fetched_since) == 12
    finally:
        event.remove(engine, "before_cursor_execute", seen)
        deletions.CHUNK = deletions_chunk
    assert not [s for s in statements if s.startswith("INSERT")]
    locks = [s for s in statements if "FOR UPDATE" in s]
    assert len(locks) == 3 and all("SKIP LOCKED" in s for s in locks)


async def test_marking_skips_a_row_a_writer_holds(pg_sessions, clock) -> None:
    """Review 4: a row another transaction holds (a page writing it, so it is being seen) is
    skipped, not waited for; the next complete fetch decides it."""
    await store(pg_sessions, HEART, heart(60), heart(61, at=T0 + MINUTE))
    fetched_since = clock.advance(hours=1)
    async with pg_sessions.begin() as writer:
        await writer.execute(
            text(
                "SELECT id FROM raw_health_data WHERE raw_json->'heartRate'->>'beatsPerMinute'"
                " = '60' FOR UPDATE"
            )
        )
        marked = await asyncio.wait_for(
            mark(pg_sessions, HEART, T0 - HOUR, T0 + HOUR, fetched_since), 5
        )
    assert marked == 1


async def test_a_batch_meeting_a_row_lock_backs_off_and_says_so(pg_sessions, monkeypatch):
    """A reconciler batch that waited for another transaction's row lock (a long import
    holding raw rows FOR UPDATE) kept every writer of the data type queued behind its
    exclusive lock for up to its statement timeout. It now gives up within seconds, says so
    (the CLI prints it) and retries."""
    await legacy(pg_sessions, HEART, heart(60), T0, last=T0 + HOUR)
    await legacy(pg_sessions, HEART, heart(60, name=NEW_NAME), T0 + 9 * HOUR)
    notices: list[str] = []
    pauses: list[float] = []

    async def pause(seconds: float) -> None:
        pauses.append(seconds)
        await asyncio.sleep(0.5)

    monkeypatch.setattr(reconciler, "_pause", pause)
    monkeypatch.setattr(reconciler.source_chains, "ROW_LOCK_TIMEOUT", "300ms")
    holder = await pg_sessions().__aenter__()
    await holder.begin()
    await holder.execute(text("SELECT id FROM raw_health_data FOR UPDATE"))
    started = time.monotonic()
    task = asyncio.create_task(identity_backfill(pg_sessions, notice=notices.append))
    try:
        await asyncio.sleep(2)
        assert notices and "retrying in 2 s" in notices[0]
        # Meanwhile a page of the data type is stored at once (no queue behind the batch).
        await asyncio.wait_for(store(pg_sessions, HEART, heart(61, at=T0 + MINUTE)), 5)
    finally:
        await holder.rollback()
        await holder.close()
    report = await asyncio.wait_for(task, 30)
    assert sum(report["heart-rate"].linked.values()) == 1 and time.monotonic() - started < 30


def _sync_url(database_url: str) -> str:
    return database_url.replace("+asyncpg", "+psycopg")


def test_downgrade_refuses_while_only_the_sightings_have_rows(database_url, alembic_config):
    """The downgrade guard skipped raw_source_sightings: dropping it lost when each source
    was seen. A database whose only identity rows are sightings now refuses too."""
    command.upgrade(alembic_config, "head")

    async def fill() -> None:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            await store(async_sessionmaker(engine, expire_on_commit=False), HEART, heart(60))
        finally:
            await engine.dispose()

    asyncio.run(fill())
    engine = create_engine(_sync_url(database_url))
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM raw_health_data_identity")) == 0
            assert connection.scalar(text("SELECT count(*) FROM raw_source_sightings")) == 1
        with pytest.raises(RuntimeError, match="raw_source_sightings holds 1 row"):
            command.downgrade(alembic_config, "20261020_0011")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20261021_0012"
            )
    finally:
        engine.dispose()


def test_verify_on_a_database_still_at_0011_says_so(database_url, alembic_config, monkeypatch):
    """This version's verify against a database not upgraded yet failed with an SQL error;
    it now names the revision and what to do."""
    from app.cli import main as cli

    command.upgrade(alembic_config, "20261020_0011")
    engine = create_async_engine(database_url, poolclass=NullPool)
    monkeypatch.setattr(cli, "SessionLocal", async_sessionmaker(engine, expire_on_commit=False))
    try:
        result = CliRunner().invoke(cli.app, ["verify"])
    finally:
        asyncio.run(engine.dispose())
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "the database is at revision 20261020_0011" in result.output
    assert "alembic upgrade" in result.output


async def test_the_analytics_views_find_deleted_rows_by_index(pg_sessions) -> None:
    """The views' anti-join read every raw row of the data types to find the few deleted
    ones (no index on source_deleted_at). A partial index holds just those rows."""
    for minute in range(30):
        await legacy(pg_sessions, STEPS, steps(5, start=T0 + minute * MINUTE), T0)
    await legacy(pg_sessions, STEPS, steps(7, start=T0 + HOUR), T0, deleted=T0 + HOUR)
    async with pg_sessions() as session:
        definition = await session.scalar(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
            {"name": "ix_raw_health_data_source_deleted"},
        )
        assert definition is not None and "WHERE (source_deleted_at IS NOT NULL)" in definition
        await session.execute(text("ANALYZE"))
        await session.execute(text("SET enable_seqscan = off"))
        plan = "\n".join(
            (await session.scalars(text("EXPLAIN SELECT count(*) FROM analytics_steps"))).all()
        )
        assert "ix_raw_health_data_source_deleted" in plan
        assert await session.scalar(text("SELECT count(*) FROM analytics_steps")) == 30


async def test_the_dashboard_total_survives_a_blip_on_the_old_form(pg_sessions, clock) -> None:
    """B1 (review 2, sh_steps): the dashboard showed 0 steps for a day whose suffixed rows
    came back after a blip had marked the rows that held them."""
    await legacy(pg_sessions, STEPS, steps(40), T0, last=T0 + HOUR)
    await legacy(pg_sessions, STEPS, steps(40, source=phone(PACKAGE)), T0 + 9 * HOUR)
    await identity_backfill(pg_sessions)
    clock.now = T0 + 30 * HOUR
    await mark(pg_sessions, STEPS, T0 - HOUR, T0 + HOUR, clock.now)
    clock.advance(hours=1)
    fetched_since = clock.now
    await store(pg_sessions, STEPS, steps(40))
    assert await mark(pg_sessions, STEPS, T0 - HOUR, T0 + HOUR, fetched_since) == 0
    async with pg_sessions() as session:
        data = await dashboard_data(session, date(2026, 10, 18), 1, "UTC")
        assert data["days"][0]["steps"] == 40
        assert await session.scalar(text("SELECT sum(count) FROM analytics_steps")) == 40
        _, errors, _ = await verify_identities(session)
    assert errors == []
