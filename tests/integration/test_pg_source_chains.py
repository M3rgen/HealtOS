"""Source chains against PostgreSQL: renames next to a second device, locks, verify, views.

Synthetic records only (tests/identity_records.py).
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest
from identity_records import (
    HEART,
    NEW_NAME,
    STEPS,
    T0,
    heart,
    legacy,
    name_of,
    phone,
    raw_rows,
    steps,
    store,
    versions_of,
)
from sqlalchemy import func, select, text

from app.models.identity import RawHealthDataSupersession
from app.models.raw_versions import RawHealthDataEvent
from app.services.dashboard import dashboard_data
from app.services.identity import verify_identities
from app.services.identity_backfill import identity_backfill
from app.services.source_chains import (
    DATA_TYPE_LOCK,
    BackfillRunning,
    _key,
    backfill_lock,
)
from app.services.storage import store_records
from app.sync.deletions import mark_source_deletions

pytestmark = pytest.mark.integration

HOUR = timedelta(hours=1)
WATCH = "Example Watch"


async def mark(sessions, spec, start, end, since) -> int:
    async with sessions.begin() as session:
        return await mark_source_deletions(session, spec, start, end, since)


async def execute(sessions, *statements: str) -> None:
    async with sessions.begin() as session:
        for statement in statements:
            await session.execute(text(statement))


async def test_rename_next_to_a_second_device_hides_nothing_and_marks_nothing(
    pg_sessions, clock
) -> None:
    """Review 2 blocker 1, with archived old/new duplicates and a second device seen alongside:
    the rule leaves the rename undecided, a third name gets a row of its own, the watch keeps
    its payload, and no deletion mark is guessed."""
    old = await legacy(pg_sessions, HEART, heart(60), T0, last=T0 + 3 * HOUR)
    new = await legacy(
        pg_sessions, HEART, heart(60, name=NEW_NAME), T0 + 9 * HOUR, last=T0 + 12 * HOUR
    )
    watch = await legacy(pg_sessions, HEART, heart(64, name=WATCH), T0, last=T0 + 13 * HOUR)
    report = await identity_backfill(pg_sessions)
    assert report["heart-rate"].chains == [] and report["heart-rate"].ambiguous
    clock.now = T0 + 20 * HOUR
    fetched_since = clock.now
    stats = await store(pg_sessions, HEART, heart(60, name="Example Band 2"), heart(64, name=WATCH))
    assert (stats.inserted, stats.updated, stats.unchanged, stats.versions_kept) == (1, 0, 1, 0)
    assert await mark(pg_sessions, HEART, T0, T0 + HOUR, fetched_since) == 0
    assert await versions_of(pg_sessions, watch) == []
    rows = {row.id: row for row in await raw_rows(pg_sessions, HEART)}
    assert name_of(rows[new]) == NEW_NAME and name_of(rows[old]) == "Example Band Pro"
    async with pg_sessions() as session:
        _, errors, notes = await verify_identities(session)
    assert errors == [] and notes == []


async def test_one_backfill_at_a_time_and_writers_share_the_data_type_lock(pg_sessions) -> None:
    """Review 1: a second identity-backfill refuses at once; a page being stored (and a
    deletion check) holds the data type's shared lock, which a backfill batch waits for."""
    await legacy(pg_sessions, HEART, heart(60), T0)
    async with backfill_lock(pg_sessions):
        with pytest.raises(BackfillRunning, match="another identity-backfill is running"):
            await identity_backfill(pg_sessions)
    await identity_backfill(pg_sessions)  # released

    params = {"namespace": DATA_TYPE_LOCK, "key": _key("heart-rate")}

    async def exclusive_free() -> bool:
        async with pg_sessions.begin() as session:
            return bool(
                await session.scalar(
                    text("SELECT pg_try_advisory_xact_lock(:namespace, :key)"), params
                )
            )

    async with pg_sessions.begin() as writer:
        await store_records(writer, HEART, [heart(61, at=T0 + HOUR)])
        assert await exclusive_free() is False
        async with pg_sessions.begin() as other:  # other writers go ahead side by side
            await store_records(other, STEPS, [steps(5)])
            await mark_source_deletions(other, HEART, T0, T0 + HOUR, T0)
    assert await exclusive_free() is True

    # A backfill started while a page is open waits for it, then completes.
    async with pg_sessions.begin() as writer:
        await store_records(writer, HEART, [heart(62, at=T0 + 2 * HOUR)])
        task = asyncio.create_task(identity_backfill(pg_sessions, data_types=["heart-rate"]))
        await asyncio.sleep(0.5)
        assert not task.done()
    report = await asyncio.wait_for(task, 30)
    assert report["heart-rate"].rows == 3


async def test_verify_reports_bad_links_chains_constraints_and_rules(pg_sessions) -> None:
    """Review 2: cycles of any length, links hiding a named record, links across data types
    or devices, links behind a deleted row, chain cycles, and tampered constraints or rules."""
    band = [await legacy(pg_sessions, HEART, heart(60 + n, at=T0 + n * HOUR), T0) for n in range(3)]
    watch = await legacy(pg_sessions, HEART, heart(90, name=WATCH), T0)
    step = await legacy(pg_sessions, STEPS, steps(5), T0)
    named = await legacy(
        pg_sessions,
        HEART,
        {**heart(70, at=T0 + 5 * HOUR), "name": "users/me/dataTypes/heart-rate/dataPoints/x"},
        T0,
    )
    gone = await legacy(pg_sessions, HEART, heart(71, at=T0 + 6 * HOUR), T0, deleted=T0)
    alive = await legacy(pg_sessions, HEART, heart(71, at=T0 + 6 * HOUR, name=NEW_NAME), T0)

    def link(old, new, data_type="heart-rate") -> str:
        return (
            "INSERT INTO raw_health_data_supersessions (old_raw_id, new_raw_id, data_type, "
            "old_google_data_point_id, new_google_data_point_id, reason) "
            f"SELECT o.id, n.id, '{data_type}', o.google_data_point_id, n.google_data_point_id, "
            f"'duplicate' FROM raw_health_data o, raw_health_data n "
            f"WHERE o.id = '{old}' AND n.id = '{new}'"
        )

    async with pg_sessions() as session:
        assert (await verify_identities(session))[1] == []
    await execute(
        pg_sessions,
        link(band[0], band[1]),
        link(band[1], band[2]),
        link(band[2], band[0]),  # a 3-cycle
        link(named, watch),
        link(step, watch, "steps"),
        link(alive, gone),
        "INSERT INTO raw_source_chains (data_type, from_fingerprint, to_fingerprint, from_label, "
        "to_label, decided_by, evidence, decided_at) VALUES "
        "('heart-rate', 'a', 'b', 'A', 'B', 'live', '{}', now()), "
        "('heart-rate', 'b', 'a', 'B', 'A', 'live', '{}', now())",
        "ALTER TABLE raw_source_chains DROP CONSTRAINT uq_raw_source_chains_data_type",
        "CREATE RULE keep_quiet AS ON DELETE TO raw_health_data_identity DO INSTEAD NOTHING",
    )
    async with pg_sessions() as session:
        _, errors, _ = await verify_identities(session)
    text_ = "\n".join(errors)
    for expected in (
        f"supersession of raw row {band[0]} forms a cycle",
        f"supersession of raw row {band[0]} points at a superseded row",
        f"supersession of raw row {named} hides a record with a Google id",
        f"supersession of raw row {step} does not match its rows",
        f"supersession of raw row {named} links two different records",
        f"supersession of raw row {alive} hides a live record behind a deleted one",
        f"supersession of raw row {alive} links two different sources",
        "source chain of heart-rate from a forms a cycle",
        "constraint missing: raw_source_chains.uq_raw_source_chains_data_type",
        "rule keep_quiet on guarded table raw_health_data_identity",
    ):
        assert expected in text_, (expected, errors)


async def test_analytics_views_count_what_the_dashboard_counts(pg_sessions) -> None:
    """Review 2: the analytics_* views leave out source-deleted and superseded rows."""
    await legacy(pg_sessions, STEPS, steps(40), T0)
    await legacy(pg_sessions, STEPS, steps(40, source=phone("com.example.health")), T0 + 24 * HOUR)
    await legacy(pg_sessions, STEPS, steps(7, start=T0 + HOUR), T0, deleted=T0 + HOUR)

    async def counted() -> tuple:
        async with pg_sessions() as session:
            data = await dashboard_data(session, date(2026, 10, 18), 1, "UTC")
            view = await session.scalar(text("SELECT sum(count) FROM analytics_steps"))
        return data["days"][0]["steps"], float(view)

    assert await counted() == (80, 80.0)  # the deleted 7 is left out by both
    await identity_backfill(pg_sessions)
    assert await counted() == (40, 40.0)


async def test_backfill_never_chains_links(pg_sessions, clock) -> None:
    """Review 1: no A->B and B->A, no link to a superseded row: a later duplicate of a
    record already held by a linked row is linked to that row, not the other way round."""
    old = await legacy(pg_sessions, HEART, heart(60), T0, last=T0 + HOUR)
    new = await legacy(pg_sessions, HEART, heart(60, name=NEW_NAME), T0 + 9 * HOUR)
    await identity_backfill(pg_sessions)
    third = await legacy(
        pg_sessions, HEART, heart(60, name="Example Band 2"), T0 + 30 * HOUR, last=T0 + 31 * HOUR
    )
    await identity_backfill(pg_sessions)
    async with pg_sessions() as session:
        found = {
            row.old_raw_id: row.new_raw_id
            for row in (await session.scalars(select(RawHealthDataSupersession))).all()
        }
        _, errors, _ = await verify_identities(session)
        merged = await session.scalar(
            select(func.count()).where(RawHealthDataEvent.event == "identity-merged")
        )
    assert found == {old: new, third: new} and errors == [] and merged == 2
