"""PostgreSQL side of review 7 (lane AC): the concurrent index build is never cancelled by a
statement timeout, and a deferral whose row a writer held stays open. Synthetic data only."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from alembic import command
from identity_records import HEART, T0, heart, legacy, mark, raw_rows, store
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine

from app.models.identity import IdentityDeferredDeletion
from app.services.identity_backfill import identity_backfill
from app.sync.deletions import deferred_deletions

pytestmark = pytest.mark.integration

HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
A1, WATCH = "Band A1", "Example Watch"


def _sync_url(database_url: str) -> str:
    return database_url.replace("+asyncpg", "+psycopg")


def test_the_deleted_index_build_has_no_statement_timeout(database_url, alembic_config) -> None:
    """Review 7: the concurrent build set only lock_timeout = 0; a statement_timeout of the
    role or database could cancel it half way, leaving an invalid index. Neither applies."""
    command.upgrade(alembic_config, "20261020_0011")
    engine = create_engine(_sync_url(database_url))
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            name = connection.scalar(text("SELECT current_database()"))
            connection.execute(text(f"ALTER DATABASE \"{name}\" SET statement_timeout = '1min'"))
            connection.execute(text(f"ALTER DATABASE \"{name}\" SET lock_timeout = '1min'"))
    finally:
        engine.dispose()
    during: list[tuple[str, str]] = []

    def seen(_connection, cursor, statement, *_args) -> None:
        if statement.startswith("CREATE INDEX CONCURRENTLY"):
            settings = cursor.connection.execute(
                "SELECT current_setting('statement_timeout'), current_setting('lock_timeout')"
            ).fetchone()
            during.append(tuple(settings))

    event.listen(Engine, "before_cursor_execute", seen)
    try:
        command.upgrade(alembic_config, "head")
    finally:
        event.remove(Engine, "before_cursor_execute", seen)
    assert during == [("0", "0")]


async def test_a_deferral_whose_row_a_writer_held_stays_open(pg_sessions, clock) -> None:
    """Review 7: marking skips a row another transaction holds (it is being seen), deciding
    nothing about it, yet its open deferral was resolved. It stays open for the next
    complete fetch."""
    keys = [T0 + index * MINUTE for index in range(3)]
    for at in keys:
        await legacy(pg_sessions, HEART, heart(60, at=at, name=A1), at)
    await identity_backfill(pg_sessions)
    clock.now = since = T0 + 6 * HOUR
    await store(pg_sessions, HEART, *(heart(60, at=at, name=WATCH) for at in keys))
    await mark(pg_sessions, HEART, T0, T0 + HOUR, since)
    async with pg_sessions() as session:
        assert [entry["deferred"] for entry in await deferred_deletions(session)] == [3]
    held = next(
        row.id
        for row in await raw_rows(pg_sessions, HEART)
        if row.raw_json["dataSource"]["device"]["displayName"] == A1
        and row.source_start_time == keys[0]
    )
    since = clock.advance(minutes=10)
    async with pg_sessions.begin() as writer:
        await writer.execute(
            text("SELECT id FROM raw_health_data WHERE id = :id FOR UPDATE"), {"id": held}
        )
        marked = await asyncio.wait_for(mark(pg_sessions, HEART, T0, T0 + HOUR, since), 5)
    assert marked == 5
    async with pg_sessions() as session:
        still = (
            await session.scalars(
                select(IdentityDeferredDeletion.raw_id).where(
                    IdentityDeferredDeletion.resolved_at.is_(None)
                )
            )
        ).all()
    assert still == [held]
