"""PostgreSQL side of review 8 (lane AD): deletion marking after a decided chain stays linear in
the window (AD2), a complete fetch never waits for another one's deferrals (AD3), and resolving
the deferrals of a window compares no deferral with every row of it (AE). Synthetic data only."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta

import pytest
from identity_records import HEART, T0, heart, legacy, mark, store
from sqlalchemy import event, select, text

from app.models.identity import IdentityDeferredDeletion
from app.models.state import RawHealthData
from app.parsers.common import payload_hash, source_id, temporal
from app.services.identity_backfill import identity_backfill
from app.sync.deletions import deferred_deletions, mark_source_deletions

pytestmark = pytest.mark.integration

HOUR = timedelta(hours=1)
SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)
A1, A2, WATCH = "Band A1", "Band A2", "Example Watch"
# More candidates than one chunk of marking (app/sync/deletions.py CHUNK).
SAMPLES = 2500


async def archive(sessions, records, seen) -> None:
    """Rows as stored before v2 (their v1 ids), in one transaction."""
    async with sessions.begin() as session:
        for record in records:
            start, end, zone = temporal(HEART, record)
            session.add(
                RawHealthData(
                    id=uuid.uuid4(),
                    google_data_point_id=source_id(HEART, record),
                    data_type=HEART.name,
                    source_start_time=start,
                    source_end_time=end,
                    source_timezone=zone,
                    raw_json=record,
                    payload_hash=payload_hash(record),
                    first_seen_at=seen,
                    last_seen_at=seen,
                )
            )


def removed_by_join_filters(plan: dict) -> int:
    """Rows every join of an ``EXPLAIN ANALYZE`` plan compared and discarded, in all."""
    total = int(plan.get("Rows Removed by Join Filter", 0)) * int(plan.get("Actual Loops", 1))
    return total + sum(removed_by_join_filters(child) for child in plan.get("Plans", ()))


async def test_marking_after_a_decided_chain_compares_no_candidate_with_every_holder(
    pg_sessions, clock
) -> None:
    """Review 7 AD2: the check for a held row (superseded by a row this fetch saw unchanged) was
    an anti-join the planner estimated at one row, so it compared every candidate with every
    holder: a 7-day window took 26 s instead of 2, and doubling the window quadrupled it. Every
    statement of the marking now does work in proportion to the candidates."""
    times = [T0 + index * SECOND for index in range(SAMPLES)]
    await archive(pg_sessions, [heart(60, at=at, name=A1) for at in times], T0 + HOUR)
    await identity_backfill(pg_sessions)
    clock.now = T0 + 2 * HOUR
    renamed = [heart(60, at=at, name=A2) for at in times]
    for offset in range(0, SAMPLES, 1000):
        await store(pg_sessions, HEART, *renamed[offset : offset + 1000])
    clock.now = T0 + 5 * HOUR
    await identity_backfill(pg_sessions, incremental=True)
    clock.now = since = T0 + 6 * HOUR
    for offset in range(0, SAMPLES, 1000):
        await store(pg_sessions, HEART, *renamed[offset : offset + 1000])
    async with pg_sessions.begin() as session:
        await session.execute(text("ANALYZE"))
        held = await session.scalar(text("SELECT count(*) FROM raw_health_data_supersessions"))
    assert held == SAMPLES
    async with pg_sessions.begin() as session:
        connection = await session.connection()
        statements: list[tuple[str, tuple]] = []

        def seen(_connection, _cursor, statement, parameters, *_args) -> None:
            if statement.lstrip().upper().startswith("SELECT") and "FOR UPDATE" not in statement:
                statements.append((statement, tuple(parameters or ())))

        event.listen(connection.sync_connection, "before_cursor_execute", seen)
        try:
            marked = await mark_source_deletions(session, HEART, T0, T0 + HOUR, since)
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", seen)
        assert marked == 0
        driver = (await connection.get_raw_connection()).driver_connection
        assert driver is not None
        worst = 0
        for statement, parameters in statements:
            found = await driver.fetchval(
                f"EXPLAIN (ANALYZE, FORMAT JSON) {statement}", *parameters
            )
            plan = (json.loads(found) if isinstance(found, str) else found)[0]["Plan"]
            worst = max(worst, removed_by_join_filters(plan))
        await session.rollback()
    assert worst <= 2 * SAMPLES, worst


async def test_a_complete_fetch_never_waits_for_another_ones_deferrals(pg_sessions, clock) -> None:
    """Review 7 AD3: complete fetches of overlapping windows deferred the same rows; each
    upsert of a deferral waited for the other fetch's transaction (20-55 s behind a slow or
    killed one). The second fetch waits for nothing; the first one's deferrals are kept."""
    keys = [T0 + index * MINUTE for index in range(3)]
    for at in keys:
        await legacy(pg_sessions, HEART, heart(60, at=at, name=A1), at)
    await identity_backfill(pg_sessions)
    clock.now = since = T0 + 6 * HOUR
    await store(pg_sessions, HEART, *(heart(60, at=at, name=WATCH) for at in keys))
    async with pg_sessions.begin() as first:
        assert await mark_source_deletions(first, HEART, T0, T0 + HOUR, since) == 0
        second = await asyncio.wait_for(mark(pg_sessions, HEART, T0 - HOUR, T0 + HOUR, since), 5)
        assert second == 0
    async with pg_sessions() as session:
        assert [entry["deferred"] for entry in await deferred_deletions(session)] == [3]


async def test_resolving_a_windows_deferrals_compares_no_deferral_with_the_window(
    pg_sessions, clock
) -> None:
    """Review 8 AE: the deferrals of a window were resolved with ``raw_id IN (the window's
    rows)``; the planner estimated both at one row and rescanned the window for every open
    deferral (4-47 s with the sync state locked). Each deferral's row is looked up now, and
    exactly the same deferrals are resolved: those of rows in the window this fetch decided
    (returned, or marked); not those it deferred again, nor a row a writer held, nor those of
    another window."""
    # More open deferrals than one chunk of lookups (app/sync/deletions.py CHUNK).
    third = 700
    times = [T0 + index * SECOND for index in range(3 * third)]
    late = [T0 + 3 * HOUR + index * MINUTE for index in range(5)]
    async with pg_sessions.begin() as session:
        # Statistics as stale as between two autovacuum runs: nothing estimates the window.
        for table in ("raw_health_data", "identity_deferred_deletions"):
            await session.execute(text(f"ALTER TABLE {table} SET (autovacuum_enabled = false)"))
    await archive(pg_sessions, [heart(60, at=at, name=A1) for at in (*times, *late)], T0 + HOUR)
    await identity_backfill(pg_sessions)
    async with pg_sessions() as session:
        rows = dict(
            (await session.execute(select(RawHealthData.source_start_time, RawHealthData.id)))
            .tuples()
            .all()
        )
    again, returned, gone = times[:third], times[third : 2 * third], times[2 * third :]
    # The watch returns the band's readings: every band row is deferred, in two windows.
    clock.now = since = T0 + 6 * HOUR
    watch = [heart(60, at=at, name=WATCH) for at in (*times, *late)]
    for offset in range(0, len(watch), 1000):
        await store(pg_sessions, HEART, *watch[offset : offset + 1000])
    assert await mark(pg_sessions, HEART, T0, T0 + HOUR, since) == 0
    assert await mark(pg_sessions, HEART, T0 + 3 * HOUR, T0 + 4 * HOUR, since) == 0
    async with pg_sessions() as session:
        deferred = set(await session.scalars(select(IdentityDeferredDeletion.raw_id)))
    assert deferred == {rows[at] for at in (*times, *late)}
    # The next complete fetch of the first window: the watch returns ``again`` (deferred again),
    # the band ``returned`` (the watch's rows of them are deferred now), nobody ``gone`` (marked;
    # but a writer holds one of the band's rows: skipped, its deferral stays open).
    clock.now = since = T0 + 7 * HOUR
    await store(pg_sessions, HEART, *(heart(60, at=at, name=WATCH) for at in again))
    await store(pg_sessions, HEART, *(heart(60, at=at, name=A1) for at in returned))
    held = rows[gone[0]]

    async def fetch(replay: bool) -> tuple[int, int]:
        """Mark the window: committed, or (``replay``) every statement of it explained and
        analyzed in order, then rolled back. Returns (marked, rows its joins discarded)."""
        statements: list[tuple[str, tuple, bool]] = []
        async with pg_sessions.begin() as session:
            connection = await session.connection()

            def seen(_connection, _cursor, statement, parameters, _context, many) -> None:
                statements.append((statement, parameters, many))

            event.listen(connection.sync_connection, "before_cursor_execute", seen)
            try:
                marked = await mark_source_deletions(session, HEART, T0, T0 + HOUR, since)
            finally:
                event.remove(connection.sync_connection, "before_cursor_execute", seen)
            if not replay:
                return marked, 0
            await session.rollback()
        async with pg_sessions.begin() as session:
            # Through the session first: the driver's statements run in its transaction.
            await session.execute(text("SELECT 1"))
            connection = await session.connection()
            driver = (await connection.get_raw_connection()).driver_connection
            assert driver is not None
            discarded = 0
            for statement, parameters, many in statements:
                if many:
                    await driver.executemany(statement, parameters)
                    continue
                if not statement.lstrip().upper().startswith(("SELECT", "UPDATE", "INSERT")):
                    await driver.execute(statement, *(parameters or ()))
                    continue
                found = await driver.fetchval(
                    f"EXPLAIN (ANALYZE, FORMAT JSON) {statement}", *(parameters or ())
                )
                plan = (json.loads(found) if isinstance(found, str) else found)[0]["Plan"]
                discarded += removed_by_join_filters(plan)
            await session.rollback()
        return marked, discarded

    async with pg_sessions() as writer:
        await writer.execute(
            select(RawHealthData.id).where(RawHealthData.id == held).with_for_update()
        )
        marked, discarded = await fetch(replay=True)
        assert discarded <= 2 * len(times), discarded
        assert await fetch(replay=False) == (marked, 0)
        await writer.rollback()
    async with pg_sessions() as session:
        resolved = {
            raw_id: at
            for raw_id, at in (
                await session.execute(
                    select(IdentityDeferredDeletion.raw_id, IdentityDeferredDeletion.resolved_at)
                )
            ).tuples()
        }
        watch_rows = set(await session.scalars(select(RawHealthData.id))) - set(rows.values())
    band = {at: rows[at] for at in times}
    assert marked == 2 * third - 1
    assert {raw_id for raw_id, at in resolved.items() if at is not None} == {
        band[at] for at in (*returned, *gone[1:])
    }
    assert len(set(resolved.values()) - {None}) == 1
    still_open = {raw_id for raw_id, at in resolved.items() if at is None}
    assert {raw_id for raw_id in still_open if raw_id not in watch_rows} == {
        band[at] for at in again
    } | {held} | {rows[at] for at in late}
    assert len(still_open & watch_rows) == third
