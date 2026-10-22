"""PostgreSQL side of review 8 (lane AD): deletion marking after a decided chain stays linear in
the window (AD2), and a complete fetch never waits for another one's deferrals (AD3). Synthetic
data only."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta

import pytest
from identity_records import HEART, T0, heart, legacy, mark, store
from sqlalchemy import event, text

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
