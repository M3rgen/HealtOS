"""Real PostgreSQL: single-slot pools, idle session locks and independent watchdogs."""

import asyncio
import multiprocessing
from datetime import UTC, datetime

import pytest
from reconciliation_records import Client, service
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_pg_sync_ownership import receive
from test_watchdog import _watchdog

from app.services.watchdog import _ADVISORY_LOCK_ID

pytestmark = pytest.mark.integration


def worker(url, pipe):
    async def run():
        engine = create_async_engine(url, pool_size=1, max_overflow=0, pool_timeout=1)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        class Google(Client):
            async def list_data_points(self, *args, **kwargs):
                async with sessions() as session:
                    assert await session.scalar(text("SELECT 1")) == 1
                pipe.send("google")
                ending = await asyncio.to_thread(pipe.recv)
                if ending == "failed":
                    raise RuntimeError("synthetic Google failure")
                if ending == "cancelled":
                    raise asyncio.CancelledError
                return await super().list_data_points(*args, **kwargs)

        try:
            owner, _ = _watchdog(sessions, datetime.now(UTC), service(sessions, Google()))
            try:
                outcome = (await owner.periodic_check()).action
            except asyncio.CancelledError:
                outcome = "cancelled"
            assert asyncio.all_tasks() == {asyncio.current_task()}
            pipe.send(outcome)
            assert await asyncio.to_thread(pipe.recv) == "exit"
        finally:
            await engine.dispose()
            pipe.close()

    asyncio.run(run())


@pytest.mark.parametrize("ending", ["completed", "failed", "cancelled"])
async def test_recovery_ownership_lifecycle_with_one_application_slot(pg_sessions, ending):
    parent, child = multiprocessing.get_context("spawn").Pipe()
    process = multiprocessing.get_context("spawn").Process(
        target=worker,
        args=(pg_sessions.kw["bind"].url.render_as_string(hide_password=False), child),
    )
    process.start()
    child.close()
    try:
        assert await receive(parent) == "google"
        async with pg_sessions() as session:
            lock = (
                await session.execute(
                    text(
                        "SELECT l.pid, a.xact_start FROM pg_locks l "
                        "JOIN pg_stat_activity a ON a.pid = l.pid "
                        "WHERE l.locktype = 'advisory' AND l.objsubid = 1 AND l.granted "
                        "AND l.classid = :high AND l.objid = :low"
                    ),
                    {"high": _ADVISORY_LOCK_ID >> 32, "low": _ADVISORY_LOCK_ID % 2**32},
                )
            ).one()
            assert lock.xact_start is None
        contender, runner = _watchdog(pg_sessions, datetime.now(UTC))
        assert (await contender.periodic_check()).action == "overlap"
        assert runner.calls == []
        parent.send(ending)
        assert await receive(parent) == ending
        async with pg_sessions() as session:
            assert not await session.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = :pid)"),
                {"pid": lock.pid},
            )
        async with contender._global_lock() as acquired:
            assert acquired
        parent.send("exit")
        await asyncio.to_thread(process.join, 10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.kill()
        await asyncio.to_thread(process.join, 10)
        parent.close()
