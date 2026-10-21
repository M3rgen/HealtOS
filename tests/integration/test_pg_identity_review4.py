"""Record identity, fourth review: the findings that need PostgreSQL (synthetic records only).

A wrong link undone in the analytics views (Z3), dry runs that take no lock writers wait for,
a reconciler that never queues writers behind it while it waits for its lock, and verify
robust against PostgreSQL 18's NOT NULL constraints and columns added to normalized tables.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from identity_records import HEART, NEW_NAME, OLD_NAME, T0, fitbit, heart, legacy, store
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.parsers.identity import source_fingerprint
from app.services import identity_backfill as reconciler
from app.services.identity import verify_identities, verify_views
from app.services.identity_backfill import identity_backfill
from app.services.identity_decisions import decide_sources
from app.services.raw_history import verify_guards
from app.services.source_chains import lock_data_type

pytestmark = pytest.mark.integration

HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
CHARGE = "Example Charge"
OLD = source_fingerprint(fitbit(OLD_NAME))
CHARGE_FP = source_fingerprint(fitbit(CHARGE))


async def viewed(sessions) -> int:
    async with sessions() as session:
        return await session.scalar(text("SELECT count(*) FROM analytics_heart_rate")) or 0


async def test_a_wrong_link_is_undone_in_the_analytics_views(pg_sessions) -> None:
    """Z3 (f3 c6): after a wrong owner ``link``, ``reject`` and ``distinct`` left
    analytics_heart_rate at half its rows. The views count a link only while it is in force;
    verify checks the devices the views read and is green."""
    for minute in range(4):
        at = T0 + minute * MINUTE
        await store(pg_sessions, HEART, heart(60 + minute, at=at))
        await store(pg_sessions, HEART, heart(60 + minute, at=at, name=CHARGE))
    await identity_backfill(pg_sessions)
    assert await viewed(pg_sessions) == 8
    await decide_sources(pg_sessions, "heart-rate", "link", OLD[:12], CHARGE_FP[:12])
    assert await viewed(pg_sessions) == 4
    await decide_sources(pg_sessions, "heart-rate", "reject", OLD[:12], CHARGE_FP[:12])
    assert await viewed(pg_sessions) == 8
    await decide_sources(pg_sessions, "heart-rate", "distinct", OLD[:12], CHARGE_FP[:12])
    assert await viewed(pg_sessions) == 8
    async with pg_sessions() as session:
        _, errors, _ = await verify_identities(session)
    assert errors == []
    # A tampered device log would make the views count wrongly: verify says so.
    async with pg_sessions.begin() as session:
        await session.execute(
            text(
                "INSERT INTO raw_source_devices (data_type, fingerprint, device, decided_at) "
                "SELECT data_type, fingerprint, 'x', now() FROM raw_source_devices"
            )
        )
    async with pg_sessions() as session:
        _, errors, _ = await verify_identities(session)
    assert any("raw_source_devices of heart-rate does not match" in error for error in errors)


async def test_a_dry_run_takes_no_lock_writers_wait_for(pg_sessions) -> None:
    """``identity-decide link --dry-run`` (and ``identity-backfill --dry-run``) settled the
    whole data type under exclusive locks. A dry run reads what is committed: it runs while
    a page holds the data type's lock, and writes nothing."""
    await legacy(pg_sessions, HEART, heart(60), T0, last=T0 + HOUR)
    await legacy(pg_sessions, HEART, heart(60, name=CHARGE), T0 + 30 * MINUTE)
    await identity_backfill(pg_sessions)  # seen alongside: left open
    async with pg_sessions.begin() as writer:
        await lock_data_type(writer, HEART.name, exclusive=False)  # a page being stored
        outcome = await asyncio.wait_for(
            decide_sources(
                pg_sessions, "heart-rate", "link", OLD[:12], CHARGE_FP[:12], dry_run=True
            ),
            10,
        )
        report = await asyncio.wait_for(identity_backfill(pg_sessions, dry_run=True), 10)
    assert sum(outcome.stats.linked.values()) == 1 and report["heart-rate"].error is None
    async with pg_sessions() as session:
        for table in ("raw_source_decisions", "raw_source_chains", "raw_health_data_supersessions"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def test_writers_do_not_queue_behind_a_reconciler_waiting_for_its_lock(
    pg_sessions, monkeypatch
) -> None:
    """Syncs stalled up to ten seconds whenever a reconciler batch waited for the exclusive
    lock (a waiting exclusive request queues every new shared one). The reconciler now tries
    for the lock without queueing: a second page goes ahead at once meanwhile."""
    await legacy(pg_sessions, HEART, heart(60), T0)

    async def pause(_seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(reconciler, "_pause", pause)
    monkeypatch.setattr(reconciler, "LOCK_TIMEOUT", "3s")
    async with pg_sessions.begin() as first:
        await lock_data_type(first, HEART.name, exclusive=False)  # a long page
        task = asyncio.create_task(identity_backfill(pg_sessions, data_types=["heart-rate"]))
        await asyncio.sleep(0.5)  # the batch is trying for its lock now
        async with pg_sessions.begin() as second:
            await second.execute(text("SET LOCAL lock_timeout = '500ms'"))
            await lock_data_type(second, HEART.name, exclusive=False)  # not queued
        assert not task.done()
    report = await asyncio.wait_for(task, 30)
    assert report["heart-rate"].error is None and report["heart-rate"].rows == 1


async def test_verify_ignores_not_null_constraints_and_new_columns(pg_sessions) -> None:
    """verify listed every constraint of the guarded tables: a constraint that is no key or
    check (PostgreSQL 18 lists NOT NULL ones; a constraint trigger) was "unexpected". And
    ``SELECT *`` views keep the columns they were created with, so a column added to a
    normalized table later made verify fail for good."""
    await store(pg_sessions, HEART, heart(60))
    async with pg_sessions.begin() as session:
        await session.execute(
            text(
                "CREATE CONSTRAINT TRIGGER pairs_checked AFTER INSERT ON raw_source_pairs "
                "FOR EACH ROW EXECUTE FUNCTION healthos_refuse_change()"
            )
        )
        await session.execute(text("ALTER TABLE heart_rate ADD COLUMN extra integer"))
    async with pg_sessions() as session:
        _, errors, _ = await verify_identities(session)
        assert await verify_views(session) == []
        guards = await verify_guards(session)
    assert not [error for error in errors if "unexpected constraint" in error]
    assert any("pairs_checked" in error for error in guards)  # the trigger check sees it
    # A view really redefined is still found.
    async with pg_sessions.begin() as session:
        await session.execute(
            text("CREATE OR REPLACE VIEW analytics_weight AS SELECT * FROM weight")
        )
    async with pg_sessions() as session:
        assert await verify_views(session) == [
            "analytics view analytics_weight was redefined: it no longer counts what the "
            "dashboard counts (recreate it from migration 20261021_0012)"
        ]


async def test_sightings_keep_their_label_and_source_on_postgresql(pg_sessions) -> None:
    """The widen-only trigger let an UPDATE change a sighting's label or dataSource."""
    await store(pg_sessions, HEART, heart(60))
    for statement in (
        "UPDATE raw_source_sightings SET label = 'x'",
        "UPDATE raw_source_sightings SET data_source = '{}'::jsonb",
    ):
        with pytest.raises(DBAPIError, match="only widens"):
            async with pg_sessions.begin() as session:
                await session.execute(text(statement))


async def test_the_incremental_run_streams_changed_starts(pg_sessions, clock) -> None:
    """The incremental run held every changed start time in memory (hundreds of MB after
    a large import); it streams them in chunks and links all."""
    for minute in range(30):
        await legacy(pg_sessions, HEART, heart(60, at=T0 + minute * MINUTE), T0, last=T0 + HOUR)
    await identity_backfill(pg_sessions)
    clock.now = T0 + 9 * HOUR
    for minute in range(30):
        await store(pg_sessions, HEART, heart(60, at=T0 + minute * MINUTE, name=NEW_NAME))
    chunks: list[int] = []
    original = reconciler._changed_starts

    async def counting(*args):
        async for chunk in original(*args):
            chunks.append(len(chunk))
            yield chunk

    reconciler._changed_starts = counting
    try:
        report = await identity_backfill(pg_sessions, incremental=True, batch_size=100)
    finally:
        reconciler._changed_starts = original
    assert sum(report["heart-rate"].linked.values()) == 30
    assert chunks == [10, 10, 10]
