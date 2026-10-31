"""Real PostgreSQL migration, transaction and first-build invalidation fence checks."""

import asyncio
from datetime import timedelta

import pytest
from alembic import command
from derived_records import (
    ADAPTER_KINDS,
    DAY,
    RAW_MUTATIONS,
    T0,
    cache,
    fail_derived_range,
    manual,
    raw_mutation,
    read,
    sleep,
    snapshot,
    steps,
    store,
    streams,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.google_health.data_types import REGISTRY
from app.models.derived_invalidation import BUILD_LOCK
from app.models.state import WebhookEvent
from app.models.webhook_status import WebhookStatusEvent
from app.services import derived
from app.services.manual_flags import record_flag
from app.services.raw_history import verify_guards
from app.services.storage import store_records

pytestmark = pytest.mark.integration


async def test_0018_preserves_archive_history_manual_rows_and_guard_definitions(
    database_url, alembic_config
):
    command.upgrade(alembic_config, "20261025_0017")
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    guards = text(
        "SELECT t.tgname, pg_get_triggerdef(t.oid), p.prosrc, t.tgenabled FROM pg_trigger t "
        "JOIN pg_proc p ON p.oid=t.tgfoid WHERE NOT t.tgisinternal ORDER BY t.tgname"
    )
    try:
        await store(sessions, "steps", steps(count=100))
        await store(sessions, "steps", steps(count=120))
        mutation = await raw_mutation(sessions, "heart-rate", "device")
        identifier = await manual(sessions, 80)
        async with sessions() as session, session.begin():
            await session.execute(mutation)
            await record_flag(session, identifier, "test", True)
            session.add(
                WebhookEvent(
                    payload_hash="c" * 64,
                    raw_json={"synthetic": True},
                    raw_body=b' {"synthetic": true}\n',
                    signature_verified_at=T0,
                )
            )
            session.add(WebhookStatusEvent(reason="signature_valid", occurred_at=T0))
        async with sessions() as session:
            before = await snapshot(session)
            before_guards = (await session.execute(guards)).all()
        command.upgrade(alembic_config, "20261025_0019")
        async with sessions() as session:
            legacy_guards = (await session.execute(guards)).all()
        command.upgrade(alembic_config, "head")
        command.check(alembic_config)
        assert streams(await read(sessions))[0]["value"] == 120
        async with sessions() as session:
            assert await snapshot(session) == before
            assert set(before_guards) <= set((await session.execute(guards)).all())
            assert await verify_guards(session) == []
            current_guards = (await session.execute(guards)).all()
            changed = [row[0] for row in current_guards if row not in legacy_guards]
            assert set(changed) == {
                "derived_raw_health_data_insert",
                "derived_raw_health_data_update",
                "derived_raw_health_data_supersessions_insert",
                "derived_raw_source_devices_insert",
            }
            assert [(row[0], *row[2:]) for row in current_guards] == [
                (row[0], *row[2:]) for row in legacy_guards
            ]  # no function or ALWAYS status changes
        command.downgrade(alembic_config, "20261025_0019")
        async with sessions() as session:
            assert await snapshot(session) == before
            assert (await session.execute(guards)).all() == legacy_guards
        command.upgrade(alembic_config, "head")
        async with sessions() as session:
            assert await snapshot(session) == before
            assert (await session.execute(guards)).all() == current_guards
            assert await verify_guards(session) == []
        command.downgrade(alembic_config, "20261025_0017")
        async with sessions() as session:
            assert await snapshot(session) == before
            assert (await session.execute(guards)).all() == before_guards
    finally:
        await engine.dispose()


@pytest.mark.parametrize("existing_cache", [False, True])
async def test_concurrent_source_write_cannot_be_lost_during_first_or_later_build(
    pg_sessions, monkeypatch, existing_cache
):
    await store(pg_sessions, "steps", steps(count=120))
    if existing_cache:
        await read(pg_sessions)
        await store(pg_sessions, "steps", steps(count=121))
    calculated, release = asyncio.Event(), asyncio.Event()
    original = derived.canonical_day

    async def pause_after_inputs(session, day, timezone):
        result = await original(session, day, timezone)
        calculated.set()
        await asyncio.wait_for(release.wait(), 10)
        return result

    monkeypatch.setattr(derived, "canonical_day", pause_after_inputs)
    builder = asyncio.create_task(read(pg_sessions))
    await asyncio.wait_for(calculated.wait(), 10)
    writer = asyncio.create_task(store(pg_sessions, "steps", steps(count=999)))
    try:
        async with pg_sessions() as session:
            async with asyncio.timeout(10):
                while not await session.scalar(  # noqa: ASYNC110 - PostgreSQL lock observation
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks "
                        "WHERE locktype='advisory' AND NOT granted AND objid=:key)"
                    ),
                    {"key": BUILD_LOCK},
                ):  # noqa: ASYNC110 - observing a PostgreSQL lock, not a local event
                    await asyncio.sleep(0.01)
        assert not writer.done()
        release.set()
        built = await asyncio.wait_for(builder, 10)
        await asyncio.wait_for(writer, 10)
        assert streams(built)[0]["value"] == (121 if existing_cache else 120)
        row = await cache(pg_sessions)
        assert row.revision > row.built_revision
        monkeypatch.setattr(derived, "canonical_day", original)
        assert streams(await read(pg_sessions))[0]["value"] == 999
    finally:
        release.set()
        for task in (builder, writer):
            if not task.done():
                task.cancel()
        await asyncio.gather(builder, writer, return_exceptions=True)


async def test_source_and_flag_rollback_reverts_dirty_revisions_atomically(pg_sessions):
    await store(pg_sessions, "steps", steps())
    identifier = await manual(pg_sessions, 80)
    before = await read(pg_sessions)
    with pytest.raises(RuntimeError, match="rollback"):
        async with pg_sessions() as session, session.begin():
            await store_records(session, REGISTRY["steps"], [steps(count=999)])
            await record_flag(session, identifier, "test", True)
            assert (
                await session.scalar(text("SELECT revision > built_revision FROM derived_days"))
            ) is True
            raise RuntimeError("synthetic rollback")
    assert await read(pg_sessions) == before


async def _until_writer_waits(sessions):
    async with sessions() as session:
        async with asyncio.timeout(10):
            while not await session.scalar(  # noqa: ASYNC110 - PostgreSQL lock observation
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks "
                    "WHERE locktype='advisory' AND NOT granted AND objid=:key)"
                ),
                {"key": BUILD_LOCK},
            ):  # noqa: ASYNC110 - observing a PostgreSQL lock, not a local event
                await asyncio.sleep(0.01)


async def test_range_build_releases_the_lock_between_days(pg_sessions, monkeypatch):
    first = DAY - timedelta(days=1)
    await store(
        pg_sessions, "steps", steps(count=120), steps("before", start=T0 - timedelta(days=1))
    )
    original = derived.canonical_day
    entered = {first: asyncio.Event(), DAY: asyncio.Event()}
    release = {first: asyncio.Event(), DAY: asyncio.Event()}

    async def pause(session, day, timezone):
        result = await original(session, day, timezone)
        entered[day].set()
        await asyncio.wait_for(release[day].wait(), 10)
        return result

    monkeypatch.setattr(derived, "canonical_day", pause)
    builder = asyncio.create_task(derived.build_range(pg_sessions, first, DAY, "UTC"))
    await asyncio.wait_for(entered[first].wait(), 10)
    writer = asyncio.create_task(
        store(pg_sessions, "steps", steps("late", 5, T0 + timedelta(hours=2)))
    )
    try:
        await _until_writer_waits(pg_sessions)
        assert not writer.done()
        release[first].set()
        # Ingestion waits for one day's build only, not for the rest of the range (#39).
        await asyncio.wait_for(writer, 10)
        assert not builder.done()
        release[DAY].set()
        assert await asyncio.wait_for(builder, 10) == 2
        monkeypatch.setattr(derived, "canonical_day", original)
        row = await cache(pg_sessions)
        assert row.revision == row.built_revision
        assert sorted(item["value"] for item in streams(await read(pg_sessions))) == [125]
    finally:
        for event in (*entered.values(), *release.values()):
            event.set()
        for task in (builder, writer):
            if not task.done():
                task.cancel()
        await asyncio.gather(builder, writer, return_exceptions=True)


async def test_repeatable_read_snapshot_never_builds_a_cold_day(pg_sessions):
    await store(pg_sessions, "steps", steps(count=120))
    async with pg_sessions() as reader:
        await reader.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        await reader.execute(text("SELECT count(*) FROM raw_health_data"))  # snapshot taken
        # Committed after the snapshot, before the day exists: no revision is bumped.
        await store(pg_sessions, "steps", steps(count=999))
        card = await derived.derived_range(reader, DAY, DAY, "UTC")
        assert card["days"][0]["state"] == "stale" and card["days"][0]["result"] is None
        await reader.commit()
    assert await cache(pg_sessions) is None  # nothing registered or built from the snapshot
    assert streams(await read(pg_sessions))[0]["value"] == 999


async def test_interpretation_builds_before_its_snapshot(pg_sessions, monkeypatch):
    from app.services import codex_health

    monkeypatch.setattr(codex_health, "SessionLocal", pg_sessions)
    await store(pg_sessions, "sleep", sleep())
    args = {"action": "sleep", "start_date": str(DAY), "end_date": str(DAY), "timezone": "UTC"}
    result = await codex_health.read_health(args)
    assert "code" not in result and result["summaries"]
    row = await cache(pg_sessions)
    assert row.revision == row.built_revision and row.result is not None


async def test_retry_rechecks_candidate_after_waiting_for_successful_builder(
    pg_sessions, monkeypatch
):
    await fail_derived_range(pg_sessions, monkeypatch)
    original = derived.canonical_day
    calls = 0

    async def count(session, day, timezone):
        nonlocal calls
        calls += 1
        return await original(session, day, timezone)

    monkeypatch.setattr(derived, "canonical_day", count)
    async with pg_sessions() as winner, winner.begin():
        await winner.execute(text(f"SELECT pg_advisory_xact_lock({BUILD_LOCK})"))
        retry = asyncio.create_task(derived.retry_failed_range(pg_sessions, DAY, DAY, "UTC"))
        try:
            await _until_writer_waits(pg_sessions)
            await derived.rebuild_day(winner, DAY, "UTC")
            await winner.commit()
            report = await asyncio.wait_for(retry, 10)
            assert report == dict(
                candidates=1, processed=0, succeeded=0, failed=0, stale=0, skipped=1
            )
            assert calls == 1 and (await cache(pg_sessions)).build_count == 2
        finally:
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)


async def test_retry_reads_source_committed_while_waiting_for_build_lock(pg_sessions, monkeypatch):
    await store(pg_sessions, "steps", steps())
    await fail_derived_range(pg_sessions, monkeypatch)
    async with pg_sessions() as writer, writer.begin():
        await store_records(writer, REGISTRY["steps"], [steps(count=999)])
        retry = asyncio.create_task(derived.retry_failed_range(pg_sessions, DAY, DAY, "UTC"))
        try:
            await _until_writer_waits(pg_sessions)
            await writer.commit()
            assert (await asyncio.wait_for(retry, 10))["succeeded"] == 1
            row = await cache(pg_sessions)
            assert row.revision == row.built_revision
            assert streams(await read(pg_sessions))[0]["value"] == 999
        finally:
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)


async def test_retry_releases_lock_between_days_and_preserves_later_invalidation(
    pg_sessions, monkeypatch
):
    first = DAY - timedelta(days=1)
    await store(pg_sessions, "steps", steps())
    await fail_derived_range(pg_sessions, monkeypatch, first, DAY)
    original = derived.canonical_day
    entered = {day: asyncio.Event() for day in (first, DAY)}
    release = {day: asyncio.Event() for day in (first, DAY)}

    async def pause(session, day, timezone):
        result = await original(session, day, timezone)
        entered[day].set()
        await asyncio.wait_for(release[day].wait(), 10)
        return result

    monkeypatch.setattr(derived, "canonical_day", pause)
    retry = asyncio.create_task(derived.retry_failed_range(pg_sessions, first, DAY, "UTC"))
    await asyncio.wait_for(entered[first].wait(), 10)
    writer = asyncio.create_task(store(pg_sessions, "steps", steps(count=999)))
    late = None
    try:
        await _until_writer_waits(pg_sessions)
        release[first].set()
        await asyncio.wait_for(writer, 10)  # cannot wait for the entire range
        await asyncio.wait_for(entered[DAY].wait(), 10)
        late = asyncio.create_task(store(pg_sessions, "steps", steps(count=1000)))
        await _until_writer_waits(pg_sessions)
        release[DAY].set()
        assert (await asyncio.wait_for(retry, 10))["succeeded"] == 2
        await asyncio.wait_for(late, 10)
        row = await cache(pg_sessions)
        assert row.revision > row.built_revision
        monkeypatch.setattr(derived, "canonical_day", original)
        assert streams(await read(pg_sessions))[0]["value"] == 1000
    finally:
        for event in release.values():
            event.set()
        tasks = [retry, writer] + ([late] if late is not None else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_retry_refuses_repeatable_read_without_writing(pg_sessions, monkeypatch):
    await fail_derived_range(pg_sessions, monkeypatch)
    before = await cache(pg_sessions)
    engine = pg_sessions.kw["bind"].execution_options(isolation_level="REPEATABLE READ")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    report = await derived.retry_failed_range(sessions, DAY, DAY, "UTC")
    assert report["failed"] == 1 and report["succeeded"] == 0
    after = await cache(pg_sessions)
    assert (after.result, after.build_count, after.computed_at) == (
        before.result,
        before.build_count,
        before.computed_at,
    )


async def test_retry_database_abort_rolls_back_one_day_and_continues(pg_sessions, monkeypatch):
    first = DAY - timedelta(days=1)
    await fail_derived_range(pg_sessions, monkeypatch, first, DAY)
    before = await cache(pg_sessions, first)
    original = derived.canonical_day

    async def abort_first(session, day, timezone):
        if day == first:
            await session.execute(text("SELECT 1 / 0"))
        return await original(session, day, timezone)

    monkeypatch.setattr(derived, "canonical_day", abort_first)
    report = await derived.retry_failed_range(pg_sessions, first, DAY, "UTC")
    assert report == dict(candidates=2, processed=2, succeeded=1, failed=1, stale=0, skipped=0)
    after = await cache(pg_sessions, first)
    assert (after.result, after.build_count, after.computed_at) == (
        before.result,
        before.build_count,
        before.computed_at,
    )
    assert not derived.build_failed((await cache(pg_sessions)).result)


@pytest.fixture(params=["migration", "metadata"])
def fence_setup(request, database_url, alembic_config):
    """The same lock regressions run on upgrades and fresh metadata installations."""
    if request.param == "migration":
        command.upgrade(alembic_config, "head")
    else:
        from sqlalchemy import create_engine

        from app.db.base import Base

        engine = create_engine(database_url.replace("+asyncpg", "+psycopg"))
        try:
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()
    return database_url


@pytest.mark.parametrize("kind", ["heart-rate", *ADAPTER_KINDS])
@pytest.mark.parametrize("operation", RAW_MUTATIONS)
async def test_build_lock_only_fences_changed_adapter_inputs(fence_setup, kind, operation):
    engine = create_async_engine(fence_setup)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    writer = None
    try:
        mutation = await raw_mutation(sessions, kind, operation)
        last = DAY + timedelta(days=1)
        async with sessions.begin() as session:
            await derived.register_range(session, DAY, last, "UTC")
            assert await verify_guards(session) == []
        before = [(await cache(sessions, day)).revision for day in (DAY, last)]

        async def write():
            async with sessions.begin() as session:
                # A stalled unrelated write fails rather than hanging the suite.
                await session.execute(text("SET LOCAL statement_timeout = '8s'"))
                await session.execute(mutation)

        supported = kind != "heart-rate" and operation != "unchanged"
        async with sessions.begin() as builder:
            await builder.execute(text(f"SELECT pg_advisory_xact_lock({BUILD_LOCK})"))
            writer = asyncio.create_task(write())
            if supported:
                await _until_writer_waits(sessions)
                assert not writer.done()
                assert [(await cache(sessions, day)).revision for day in (DAY, last)] == before
            else:
                await asyncio.wait_for(writer, 5)  # finishes while BUILD_LOCK is still held
        await asyncio.wait_for(writer, 10)
        after = [(await cache(sessions, day)).revision for day in (DAY, last)]
        affected = [0] if supported else []
        if supported and operation in ("move", "supersession"):
            affected.append(1)
        assert [right - left for left, right in zip(before, after, strict=True)] == [
            int(index in affected) for index in range(2)
        ]
    finally:
        if writer is not None:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
        await engine.dispose()


@pytest.mark.parametrize(
    "trigger",
    [
        "derived_raw_health_data_insert",
        "derived_raw_health_data_update",
        "derived_raw_health_data_supersessions_insert",
        "derived_raw_source_devices_insert",
    ],
)
async def test_verify_detects_changed_daily_type_conditions(pg_sessions, trigger):
    from app.models.derived_invalidation import PG_TRIGGER_SQL

    table, sql = PG_TRIGGER_SQL[trigger]
    async with pg_sessions.begin() as session:
        assert await verify_guards(session) == []
        # Keep timing, function and ALWAYS intact; only remove one supported type.
        await session.execute(
            text(sql.replace("CREATE TRIGGER", "CREATE OR REPLACE TRIGGER").replace("'steps',", ""))
        )
        await session.execute(text(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {trigger}"))
        errors = await verify_guards(session)
        assert len(errors) == 1 and trigger in errors[0] and "was redefined" in errors[0]


@pytest.mark.parametrize("operation", ["manual", "flag"])
async def test_manual_writes_still_wait_for_daily_build(pg_sessions, operation):
    identifier = await manual(pg_sessions, 80)
    await read(pg_sessions)
    before = await cache(pg_sessions)

    async def write():
        if operation == "manual":
            await manual(pg_sessions, 81)
        else:
            async with pg_sessions.begin() as session:
                await record_flag(session, identifier, "test", True)

    async with pg_sessions.begin() as builder:
        await builder.execute(text(f"SELECT pg_advisory_xact_lock({BUILD_LOCK})"))
        writer = asyncio.create_task(write())
        try:
            await _until_writer_waits(pg_sessions)
            assert not writer.done()
        finally:
            await builder.rollback()
            await asyncio.wait_for(writer, 10)
    assert (await cache(pg_sessions)).revision > before.revision
