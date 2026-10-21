"""PostgreSQL side of review 5: the source-deleted index is built concurrently, verify reads
only plain views, the analytics views follow a middle reject. Synthetic data only."""

from __future__ import annotations

import asyncio
import threading
import time
from argparse import Namespace
from datetime import timedelta

from alembic import command
from identity_records import HEART, T0, fitbit, heart, legacy, store
from sqlalchemy import create_engine, text

from app.parsers.identity import source_fingerprint
from app.services.identity import verify_identities, verify_views
from app.services.identity_backfill import identity_backfill
from app.services.identity_decisions import decide_sources

HOUR = timedelta(hours=1)


async def _verify_notes(database_url: str) -> list[str]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(database_url)
    try:
        async with async_sessionmaker(engine)() as session:
            _, _, notes = await verify_identities(session)
        return notes
    finally:
        await engine.dispose()


def _sync_url(database_url: str) -> str:
    return database_url.replace("+asyncpg", "+psycopg")


def _index_valid(engine) -> bool | None:
    with engine.connect() as connection:
        return connection.scalar(
            text(
                "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = 'ix_raw_health_data_source_deleted'"
            )
        )


def test_the_deleted_index_is_built_concurrently_and_never_blocks_a_writer(
    database_url, alembic_config
) -> None:
    """0012 built the partial index inside the transaction that holds the foreign keys'
    lock: at millions of rows a writer waited. It is built CONCURRENTLY first, outside the
    transaction: a writer inserting meanwhile is not blocked; next to a snapshot held as long
    as a dump's (REPEATABLE READ) it is not started (nothing invalid is left behind), the
    upgrade goes on, verify names the index and the statement builds it later."""
    command.upgrade(alembic_config, "20261020_0011")
    engine = create_engine(_sync_url(database_url))
    try:
        dump = engine.connect().execution_options(isolation_level="REPEATABLE READ")
        dump.execute(text("SELECT count(*) FROM raw_health_data"))  # holds its snapshot
        time.sleep(5.5)  # open for a while, like a dump
        alembic_config.cmd_opts = Namespace(x=["lock-timeout=1s"])
        started = time.monotonic()
        command.upgrade(alembic_config, "head")
        assert time.monotonic() - started < 20
        dump.rollback()
        dump.close()
        assert _index_valid(engine) is None
        notes = asyncio.run(_verify_notes(database_url))
        assert any("ix_raw_health_data_source_deleted is" in note for note in notes)
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(
                text(
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_raw_health_data_source_deleted "
                    "ON raw_health_data (data_type, google_data_point_id) "
                    "WHERE source_deleted_at IS NOT NULL"
                )
            )
        assert _index_valid(engine) is True
        command.downgrade(alembic_config, "20261020_0011")
        # Without a dump, and with a writer inserting all along: built, valid, writer unblocked.
        stop = threading.Event()
        inserted: list[float] = []  # seconds each insert took

        def write() -> None:
            with engine.connect() as connection:
                while not stop.is_set():
                    begun = time.monotonic()
                    connection.execute(
                        text(
                            "INSERT INTO raw_health_data (id, google_data_point_id, data_type, "
                            "raw_json, payload_hash, first_seen_at, last_seen_at, "
                            "is_quarantined, created_at, updated_at) VALUES (gen_random_uuid(), "
                            "gen_random_uuid()::text, 'steps', '{}', 'x', now(), now(), false, "
                            "now(), now())"
                        )
                    )
                    connection.commit()
                    inserted.append(time.monotonic() - begun)
                    time.sleep(0.01)

        writer = threading.Thread(target=write)
        writer.start()
        try:
            alembic_config.cmd_opts = None
            command.upgrade(alembic_config, "head")
        finally:
            stop.set()
            writer.join()
        assert _index_valid(engine) is True
        assert inserted and max(inserted) < 5
    finally:
        alembic_config.cmd_opts = None
        engine.dispose()


async def test_verify_flags_another_sources_payload_and_a_materialized_view(pg_sessions) -> None:
    from sqlalchemy import update

    from app.models.state import RawHealthData

    raw_id = await legacy(pg_sessions, HEART, heart(60, name="Band A1"), T0)
    async with pg_sessions() as session:
        _, errors, _ = await verify_identities(session)
    assert errors == []
    async with pg_sessions.begin() as session:
        await session.execute(
            update(RawHealthData)
            .where(RawHealthData.id == raw_id)
            .values(raw_json=heart(60, name="Band A2"), payload_hash="forged")
        )
        definition = await session.scalar(
            text("SELECT pg_get_viewdef('analytics_steps'::regclass)")
        )
        await session.execute(text("DROP VIEW analytics_steps"))
        await session.execute(text(f"CREATE MATERIALIZED VIEW analytics_steps AS {definition}"))
    async with pg_sessions() as session:
        _, errors, _ = await verify_identities(session)
        view_errors = await verify_views(session)
    assert any("more than one source in their history" in error for error in errors)
    assert "analytics view missing: analytics_steps" in view_errors


async def test_the_analytics_views_follow_a_middle_reject_and_its_undo(pg_sessions, clock) -> None:
    names = ("Band A1", "Band A2", "Band B1", "Band B2")
    fingerprints = [source_fingerprint(fitbit(name))[:12] for name in names]
    await legacy(pg_sessions, HEART, heart(60, name=names[0]), T0, last=T0 + HOUR)
    await identity_backfill(pg_sessions)
    for step, name in enumerate(names[1:], start=1):
        clock.now = T0 + 5 * step * HOUR
        await store(
            pg_sessions, HEART, heart(60, name=name), heart(60 + step, at=clock.now, name=name)
        )
    await identity_backfill(pg_sessions, incremental=True)

    async def viewed() -> int:
        async with pg_sessions() as session:
            return await session.scalar(text("SELECT count(*) FROM analytics_heart_rate"))

    assert await viewed() == 4
    await decide_sources(pg_sessions, "heart-rate", "reject", fingerprints[1], fingerprints[2])
    assert await viewed() == 5  # the shared record once per device
    await decide_sources(pg_sessions, "heart-rate", "confirm", fingerprints[1], fingerprints[2])
    assert await viewed() == 4
