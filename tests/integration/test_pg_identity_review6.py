"""PostgreSQL side of review 6 (lane AB): hiders locked and checked (AB4), the upgrade's index
and locks (AB8), a JSON null dataSource (M1), plans per query (M2), a decision waiting for the
single-run lock (M3). Synthetic data only."""

from __future__ import annotations

import asyncio
import threading
import time
from argparse import Namespace
from datetime import date, timedelta

import pytest
from alembic import command
from identity_records import HEART, T0, counted_rows, heart, legacy, store
from sqlalchemy import create_engine, text

from app.google_health.data_types import REGISTRY
from app.parsers.identity import source_fingerprint
from app.services import storage
from app.services.identity import verify_identities
from app.services.identity_backfill import identity_backfill
from app.services.identity_decisions import decide_sources
from app.services.source_chains import BACKFILL_LOCK, BackfillRunning

pytestmark = pytest.mark.integration

HOUR = timedelta(hours=1)
A1, A2 = "Band A1", "Band A2"


def _sync_url(database_url: str) -> str:
    return database_url.replace("+asyncpg", "+psycopg")


async def _verify(database_url: str) -> tuple[list[str], list[str]]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(database_url)
    try:
        async with async_sessionmaker(engine)() as session:
            _, errors, notes = await verify_identities(session)
        return errors, notes
    finally:
        await engine.dispose()


def _index_valid(engine) -> bool | None:
    with engine.connect() as connection:
        return connection.scalar(
            text(
                "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = 'ix_raw_health_data_source_deleted'"
            )
        )


# --- AB8: the upgrade's partial index and locks -----------------------------------------------


def test_a_dump_begun_just_before_the_upgrade_leaves_no_invalid_index(
    database_url, alembic_config
) -> None:
    """Review 6b S5: a dump whose snapshot was younger than 5 s was not seen as busy; the
    concurrent build then waited for it until the lock timeout and left an invalid index. Any
    open snapshot leaves the build for later: nothing invalid, verify names the statement."""
    command.upgrade(alembic_config, "20261020_0011")
    engine = create_engine(_sync_url(database_url))
    try:
        dump = engine.connect().execution_options(isolation_level="REPEATABLE READ")
        dump.execute(text("SELECT count(*) FROM raw_health_data"))  # its snapshot, just begun
        alembic_config.cmd_opts = Namespace(x=["lock-timeout=1s"])
        command.upgrade(alembic_config, "head")
        dump.rollback()
        dump.close()
        assert _index_valid(engine) is None
        _, notes = asyncio.run(_verify(database_url))
        assert any("CREATE INDEX CONCURRENTLY IF NOT EXISTS" in note for note in notes)
    finally:
        alembic_config.cmd_opts = None
        engine.dispose()


def test_verify_names_an_invalid_index_left_by_an_interrupted_rebuild(
    database_url, alembic_config
) -> None:
    """Review 6a: a failed ``REINDEX ... CONCURRENTLY`` leaves an invalid ``..._ccnew`` index
    that writers keep up to date; verify did not check it."""
    command.upgrade(alembic_config, "head")
    engine = create_engine(_sync_url(database_url))
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            for _ in range(2):
                connection.execute(
                    text(
                        "INSERT INTO raw_health_data (id, google_data_point_id, data_type, "
                        "raw_json, payload_hash, first_seen_at, last_seen_at, is_quarantined, "
                        "created_at, updated_at) VALUES (gen_random_uuid(), "
                        "gen_random_uuid()::text, 'steps', '{}', 'x', now(), now(), false, "
                        "now(), now())"
                    )
                )
            with pytest.raises(Exception, match="duplicate|unique"):  # noqa: B017
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX CONCURRENTLY ix_raw_health_data_type_ccnew "
                        "ON raw_health_data (data_type)"
                    )
                )
        _, notes = asyncio.run(_verify(database_url))
        assert any(
            "index ix_raw_health_data_type_ccnew on raw_health_data is invalid" in note
            and "DROP INDEX CONCURRENTLY ix_raw_health_data_type_ccnew" in note
            for note in notes
        )
    finally:
        engine.dispose()


def test_a_reader_of_a_view_never_stalls_writers_behind_the_upgrade(
    database_url, alembic_config
) -> None:
    """Review 6a: the views were replaced last, while the transaction held the foreign keys'
    lock on raw_health_data: a reader of an analytics view stalled every writer for up to the
    lock timeout. The upgrade takes the views and that lock in one attempt, retried without
    queueing: a writer never waits behind it for long, and the upgrade goes through once the
    reader is done."""
    command.upgrade(alembic_config, "20261020_0011")
    engine = create_engine(_sync_url(database_url))
    stop = threading.Event()
    waits: list[float] = []

    def write() -> None:
        with engine.connect() as connection:
            while not stop.is_set():
                begun = time.monotonic()
                connection.execute(text("LOCK TABLE raw_health_data IN ROW EXCLUSIVE MODE"))
                connection.commit()
                waits.append(time.monotonic() - begun)
                time.sleep(0.02)

    reader = engine.connect()
    reader.execute(text("SELECT count(*) FROM analytics_heart_rate"))  # holds the view
    writer = threading.Thread(target=write)
    writer.start()
    try:
        release = threading.Timer(2.0, reader.rollback)
        release.start()
        alembic_config.cmd_opts = Namespace(x=["lock-timeout=20s"])
        started = time.monotonic()
        command.upgrade(alembic_config, "head")
        took = time.monotonic() - started
        release.join()
    finally:
        stop.set()
        writer.join()
        reader.close()
        alembic_config.cmd_opts = None
        engine.dispose()
    assert took >= 1.5  # it waited for the reader ...
    assert waits and max(waits) < 0.75  # ... without stalling the writer behind it


# --- AB4: rows that could hide the target are locked and checked -----------------------------


async def test_a_hider_revised_meanwhile_makes_the_page_resolve_again(
    pg_sessions, clock, monkeypatch
) -> None:
    """Review 6a S3: a page wrote its reading onto a row hidden behind a row showing the same
    reading; another page revised that row meanwhile, so the first page's reading ended up
    hidden behind another value. The rows that could hide a target are locked and checked."""
    await legacy(pg_sessions, HEART, heart(60, name=A1), T0, last=T0 + HOUR)
    await identity_backfill(pg_sessions)
    clock.now = T0 + 5 * HOUR
    await store(pg_sessions, HEART, heart(60, name=A2))
    clock.now = T0 + 6 * HOUR
    await identity_backfill(pg_sessions, incremental=True)
    assert len(await counted_rows(pg_sessions)) == 1
    original = storage._lock_rows
    raced: list[bool] = []

    async def racing(session, spec, google_ids, resolution):
        if not raced:
            raced.append(True)
            await store(pg_sessions, HEART, heart(61, name=A2))  # commits meanwhile
        return await original(session, spec, google_ids, resolution)

    monkeypatch.setattr(storage, "_lock_rows", racing)
    clock.advance(minutes=10)
    await store(pg_sessions, HEART, heart(60, name=A1))
    shown = sorted(
        row.raw_json["heartRate"]["beatsPerMinute"] for row in await counted_rows(pg_sessions)
    )
    assert shown == [60, 61]  # A1's 60 is returned now: never hidden behind A2's 61


# --- M1: a JSON null dataSource is no source change ------------------------------------------


async def test_a_null_and_a_missing_data_source_are_one_source(pg_sessions, database_url):
    """Review 6b t1/nullds: a record stored with "dataSource": null and then without the
    field made verify report another source's payload in the row's history, for good."""
    spec = REGISTRY["daily-resting-heart-rate"]
    day = {"year": 2026, "month": 10, "day": 3}
    await store(
        pg_sessions,
        spec,
        {"dataSource": None, "dailyRestingHeartRate": {"date": day, "beatsPerMinute": "55"}},
    )
    await store(pg_sessions, spec, {"dailyRestingHeartRate": {"date": day, "beatsPerMinute": "56"}})
    errors, _ = await _verify(database_url)
    assert errors == []


# --- M2: readers plan each query with its own parameters --------------------------------------


async def test_dashboard_queries_are_planned_with_their_parameters(pg_sessions) -> None:
    """Review 6b M2: after five executions on a long-lived connection PostgreSQL switched the
    dashboard's statements to a generic plan (heart rate 55 -> 176 ms)."""
    from app.services.dashboard import dashboard_data

    async with pg_sessions() as session:
        await dashboard_data(session, date(2026, 10, 18), 7, "UTC")
        assert await session.scalar(text("SHOW plan_cache_mode")) == "force_custom_plan"


# --- M3: a decision waits briefly for the reconciler ------------------------------------------


async def test_a_decision_waits_for_a_reconciler_run_and_names_it(pg_sessions, clock) -> None:
    """Review 6b M3: identity-decide shared the reconciler's single-run lock and was refused
    at once with "another identity-backfill is running" (54-72 refusals per run). It waits
    briefly, and a refusal names who holds the lock."""
    await legacy(pg_sessions, HEART, heart(60, name=A1), T0, last=T0 + HOUR)
    await identity_backfill(pg_sessions)
    clock.now = T0 + 5 * HOUR
    await store(pg_sessions, HEART, heart(60, name=A2))
    await identity_backfill(pg_sessions, incremental=True)
    first, second = (
        source_fingerprint(
            {"platform": "FITBIT", "recordingMethod": "DERIVED", "device": {"displayName": name}}
        )[:12]
        for name in (A1, A2)
    )
    bind = pg_sessions.kw["bind"]

    async def hold(seconds: float) -> None:
        async with bind.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(
                text("SELECT set_config('application_name', 'healthos-identity-backfill', false)")
            )
            await connection.execute(
                text("SELECT pg_advisory_lock(:a, :b)"),
                {"a": BACKFILL_LOCK[0], "b": BACKFILL_LOCK[1]},
            )
            await asyncio.sleep(seconds)
            await connection.execute(
                text("SELECT pg_advisory_unlock(:a, :b)"),
                {"a": BACKFILL_LOCK[0], "b": BACKFILL_LOCK[1]},
            )

    holder = asyncio.create_task(hold(1.5))
    await asyncio.sleep(0.3)
    outcome = await decide_sources(pg_sessions, "heart-rate", "confirm", first, second)
    await holder
    assert outcome.decision_id is not None
    holder = asyncio.create_task(hold(2.0))
    await asyncio.sleep(0.3)
    with pytest.raises(BackfillRunning, match="identity-backfill run"):
        await decide_sources(pg_sessions, "heart-rate", "reject", first, second, wait=0.5)
    await holder
