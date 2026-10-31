"""Real independent processes, PostgreSQL session loss, and durable checkpoint ownership."""

import asyncio
import multiprocessing
import zlib
from datetime import UTC, datetime, timedelta

import pytest
from retrieval_records import END, START, point
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sync_ownership_records import service, snapshot

from app.core.config import Settings
from app.google_health.client import Page
from app.google_health.data_types import REGISTRY
from app.google_health.pagination import PageBudget
from app.sync import ownership
from app.sync.backfill import BackfillService
from app.sync.incremental import STALE_RUNNING_AFTER

pytestmark = pytest.mark.integration


def worker(url, pipe, kind, start, job_id=None):
    async def run():
        # Ownership must leave this sole application connection available to OAuth and
        # response archiving, while using its own exact connection for page writes.
        engine = create_async_engine(
            url,
            pool_size=1,
            max_overflow=0,
            pool_timeout=1,
            connect_args={"server_settings": {"TimeZone": "America/Los_Angeles"}},
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        class Client:
            async def list_data_points(self, spec, lower, upper, page_token=None):
                async with sessions() as session:
                    assert await session.scalar(text("SELECT 1")) == 1
                pipe.send(("fetch", lower, upper, page_token))
                action = await asyncio.to_thread(pipe.recv)
                return Page([point(kind=kind)], "p2") if action == "more" else Page([], None)

            async def keep_after(self, responses, failure):
                pass

        async def pause():
            pipe.send(("busy",))
            assert await asyncio.to_thread(pipe.recv) == "retry"

        ownership._contended = pause
        try:
            if job_id is None:
                await service(sessions, Client()).run(kind, start, start + timedelta(days=1))
            else:
                await BackfillService(
                    Settings(_env_file=None), sessions, Client(), dict(REGISTRY)
                )._run_next_window(kind, PageBudget())
            pipe.send(("result", "completed"))
        except Exception as exc:
            pipe.send(("result", type(exc).__name__))
        finally:
            await engine.dispose()
            pipe.close()

    asyncio.run(run())


async def receive(pipe):
    def read():
        assert pipe.poll(20), "worker failed to reach the next controlled event"
        return pipe.recv()

    return await asyncio.to_thread(read)


@pytest.mark.parametrize("ending", ["completed", "terminated", "disconnected"])
async def test_process_ownership_and_restart(pg_sessions, ending):
    url = pg_sessions.kw["bind"].url.render_as_string(hide_password=False)
    context = multiprocessing.get_context("spawn")
    children = []

    def start(kind, lower):
        parent, child = context.Pipe()
        process = context.Process(target=worker, args=(url, child, kind, lower))
        process.start()
        child.close()
        children.append((process, parent))
        return process, parent

    try:
        first, owner = start("heart-rate", START)
        assert await receive(owner) == ("fetch", START, END, None)
        owner.send("more")
        assert await receive(owner) == ("fetch", START, END, "p2")
        before = await snapshot(pg_sessions)
        second, waiter = start("heart-rate", END)
        assert await receive(waiter) == ("busy",)
        assert await snapshot(pg_sessions) == before
        # An aged heartbeat does not override a live PostgreSQL owner.
        recovery = service(pg_sessions, None)
        future = datetime.now(UTC) + STALE_RUNNING_AFTER + timedelta(minutes=1)
        assert await recovery.recover_interrupted(now=future) == 0
        assert await snapshot(pg_sessions) == before

        _, other = start("steps", START)
        assert await receive(other) == ("fetch", START, END, None)
        other.send("done")
        assert await receive(other) == ("result", "completed")
        async with pg_sessions() as session:
            pid, transaction = (
                await session.execute(
                    text(
                        "SELECT l.pid, a.xact_start FROM pg_locks l "
                        "JOIN pg_stat_activity a ON a.pid = l.pid "
                        "WHERE l.locktype = 'advisory' AND l.classid = :namespace "
                        "AND l.objid = :key AND l.objsubid = 2 AND l.granted"
                    ),
                    {
                        "namespace": ownership.RUN_NAMESPACE,
                        "key": (zlib.crc32(b"heart-rate") - 2**31) % 2**32,
                    },
                )
            ).one()
        assert transaction is None  # Google I/O never holds a transaction open.
        if ending == "terminated":
            first.terminate()
            await asyncio.to_thread(first.join, 10)
            assert not first.is_alive()
        if ending != "completed":
            # PostgreSQL acknowledges backend exit before the old client can continue.
            async with pg_sessions.begin() as session:
                await session.execute(
                    text(
                        "SELECT pg_terminate_backend(:pid, 10000) "
                        "FROM pg_stat_activity WHERE pid = :pid"
                    ),
                    {"pid": pid},
                )
        if ending != "terminated":
            owner.send("done")
            result = await receive(owner)
            assert result[0] == "result"
            assert (result[1] == "completed") == (ending == "completed")
        if ending != "completed":
            # No later write from the dead/disconnected owner changed its checkpoint.
            failed = await snapshot(pg_sessions)
            assert [
                row for row in failed["retrieval_checkpoints"] if row["owner"] == "heart-rate"
            ] == before["retrieval_checkpoints"]
            heart = next(row for row in failed["sync_state"] if row["data_type"] == "heart-rate")
            assert heart["last_page_token"] == "p2"
            assert await recovery.recover_interrupted(now=future) == 1
        waiter.send("retry")
        if ending != "completed":
            assert await receive(waiter) == ("fetch", START, END, "p2")
            waiter.send("done")
        assert await receive(waiter) == ("fetch", END, END + timedelta(days=1), None)
        waiter.send("done")
        assert await receive(waiter) == ("result", "completed")
        await asyncio.to_thread(second.join, 10)
        assert second.exitcode == 0
        final = await snapshot(pg_sessions)
        heart = next(row for row in final["sync_state"] if row["data_type"] == "heart-rate")
        assert heart["last_requested_from"] == END and heart["last_page_token"] is None
        for table in ("retrieval_receipts", "retrieval_events", "raw_health_data_versions"):
            assert all(row in final[table] for row in before[table])
        resumed = [row for row in final["retrieval_receipts"] if row["parent_event_id"] is not None]
        assert [row["parent_event_id"] for row in resumed] == (
            [] if ending == "completed" else [before["retrieval_checkpoints"][0]["event_id"]]
        )
    finally:
        for process, pipe in children:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 10)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 10)
            pipe.close()


async def waiting_backends(sessions):
    async with sessions() as session:
        rows = await session.execute(
            text(
                "SELECT pid, wait_event_type FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                "AND query LIKE 'SELECT pg_advisory_lock(%'"
            )
        )
        return [tuple(row) for row in rows]


@pytest.mark.parametrize("slice_timeout", ["100ms", "2s"])
async def test_waiter_queues_on_one_backend_and_acquires_in_request_order(
    pg_sessions, monkeypatch, slice_timeout
):
    monkeypatch.setattr(
        ownership, "WAIT_SLICE", text(f"SET LOCAL lock_timeout = '{slice_timeout}'")
    )
    pool = pg_sessions.kw["bind"].sync_engine.pool
    contended = asyncio.Event()

    async def contention():
        contended.set()

    monkeypatch.setattr(ownership, "_contended", contention)

    release = asyncio.Event()

    async def wait():
        async with ownership.run_ownership(pg_sessions, "heart-rate") as sessions:
            async with sessions.begin():
                pass
            await release.wait()
            return "acquired"

    async with ownership.run_ownership(pg_sessions, "heart-rate") as owner:
        async with owner.begin():  # HELD_LOCK sees the session lock on this backend
            assert pool.checkedout() == 0  # the isolated pool, never the application's
        # A pool recreated from this one, like the waiter's, keeps its listeners.
        connects = []
        event.listen(pool, "connect", lambda *args: connects.append(args))
        task = asyncio.create_task(wait())
        await asyncio.wait_for(contended.wait(), 5)
        await waiting_backends(pg_sessions)  # the probe's own pooled connection
        opened = len(connects)
        seen = []
        for _ in range(12):  # several 100 ms slices time out and requeue
            await asyncio.sleep(0.1)
            seen += await waiting_backends(pg_sessions)
        assert opened and len(connects) == opened
        assert len({pid for pid, _ in seen}) == 1 and ("Lock" in {w for _, w in seen})
    if slice_timeout == "2s":
        # Released while the waiter is queued: a newcomer cannot jump ahead of it.
        async with ownership.run_ownership(pg_sessions, "heart-rate", wait=False) as late:
            assert late is None
    release.set()
    assert await asyncio.wait_for(task, 5) == "acquired"
    assert await waiting_backends(pg_sessions) == []
