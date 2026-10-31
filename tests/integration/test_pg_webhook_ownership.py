"""Real process/connection loss across the whole webhook notification lifetime."""

import asyncio
import multiprocessing
import zlib
from contextlib import AsyncExitStack

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_pg_sync_ownership import receive
from webhook_ownership_records import age, ingest, processor, snapshot

from app.google_health.client import Page
from app.models.state import WebhookEvent, WebhookNotification
from app.sync import ownership

pytestmark = pytest.mark.integration


def worker(url, pipe, batch, phase):
    async def run():
        engine = create_async_engine(url, pool_size=1, max_overflow=0, pool_timeout=1)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        class Client:
            async def list_data_points(self, spec, lower, upper, page_token=None):
                # Both ownership connections must leave the sole application connection free.
                async with sessions() as session:
                    assert await session.scalar(text("SELECT 1")) == 1
                pipe.send(("fetch",))
                assert await asyncio.to_thread(pipe.recv) == "done"
                return Page([], None)

            async def keep_after(self, responses, failure):
                pass

        async def pause():
            pipe.send(("busy",))
            assert await asyncio.to_thread(pipe.recv) == "retry"

        ownership._contended = pause
        runner = processor(sessions, Client())
        original = runner.incremental.run
        count = 0

        async def between_windows(*args, **kwargs):
            nonlocal count
            result = await original(*args, **kwargs)
            count += 1
            if count == 1:
                pipe.send(("gap",))
                assert await asyncio.to_thread(pipe.recv) == "resume"
            return result

        if phase == "gap":
            runner.incremental.run = between_windows
        try:
            stats = await runner.process_batch(batch)
            pipe.send(("result", "completed" if stats.completed_elements else "running"))
        except Exception as exc:
            pipe.send(("result", type(exc).__name__))
        finally:
            await engine.dispose()
            pipe.close()

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["google", "type", "gap"])
@pytest.mark.parametrize("ending", ["completed", "terminated", "disconnected"])
async def test_notification_process_ownership(pg_sessions, phase, ending):
    recovery = processor(pg_sessions, None)
    batch, digest = await ingest(recovery, windows=2 if phase == "gap" else 1)
    url = pg_sessions.kw["bind"].url.render_as_string(hide_password=False)
    context = multiprocessing.get_context("spawn")
    children = []
    blocker = AsyncExitStack()

    def start(lane):
        parent, child = context.Pipe()
        process = context.Process(target=worker, args=(url, child, batch, lane))
        process.start()
        child.close()
        children.append((process, parent))
        return process, parent

    async def finish(pipe):
        message = await receive(pipe)
        while message == ("fetch",):
            pipe.send("done")
            message = await receive(pipe)
        assert message == ("result", "completed")

    try:
        if phase == "type":
            await blocker.enter_async_context(ownership.run_ownership(pg_sessions, "heart-rate"))
        first, owner = start(phase)
        message = await receive(owner)
        if phase == "gap":
            assert message == ("fetch",)
            owner.send("done")
            message = await receive(owner)
        assert message == ({"google": ("fetch",), "type": ("busy",), "gap": ("gap",)}[phase])
        await age(pg_sessions)
        before = await snapshot(pg_sessions)
        assert await recovery.recover_running() == 0
        assert (await recovery.process_pending()).completed_elements == 0
        assert await snapshot(pg_sessions) == before
        async with pg_sessions() as session:
            locks = (
                await session.execute(
                    text(
                        "SELECT l.pid, l.classid, a.xact_start FROM pg_locks l "
                        "JOIN pg_stat_activity a ON a.pid = l.pid "
                        "WHERE l.locktype = 'advisory' AND l.objsubid = 2 AND l.granted "
                        "AND ((l.classid = :notification AND l.objid = :element) "
                        "OR (l.classid = :run AND l.objid = :kind))"
                    ),
                    {
                        "notification": ownership.NOTIFICATION_NAMESPACE,
                        "element": (zlib.crc32(digest.encode()) - 2**31) % 2**32,
                        "run": ownership.RUN_NAMESPACE,
                        "kind": (zlib.crc32(b"heart-rate") - 2**31) % 2**32,
                    },
                )
            ).all()
        assert len(locks) == (1 if phase == "gap" else 2)
        assert all(lock.xact_start is None for lock in locks)
        if ending == "completed":
            await blocker.aclose()
            owner.send({"google": "done", "type": "retry", "gap": "resume"}[phase])
            await finish(owner)
            assert await recovery.recover_running() == 0
        else:
            if ending == "terminated":
                first.terminate()
                await asyncio.to_thread(first.join, 10)
                assert not first.is_alive()
            # For a lost process both leases disappear; during a type wait its blocker
            # belongs to another process and must remain alive until explicitly released.
            async with pg_sessions.begin() as session:
                for lock in locks:
                    if phase != "type" or lock.classid == ownership.NOTIFICATION_NAMESPACE:
                        await session.execute(
                            text(
                                "SELECT pg_terminate_backend(:pid, 10000) "
                                "FROM pg_stat_activity WHERE pid = :pid"
                            ),
                            {"pid": lock.pid},
                        )
            assert await recovery.recover_running() == 1
            await blocker.aclose()
            _, replacement = start("google")
            await finish(replacement)
            final = await snapshot(pg_sessions)
            if ending == "disconnected":
                # Only let the obsolete owner return AFTER its replacement committed.
                owner.send({"google": "done", "type": "retry", "gap": "resume"}[phase])
                result = await receive(owner)
                assert result[0] == "result" and result[1] != "completed"
                after = await snapshot(pg_sessions)
                for table in final.keys() - {"webhook_status_events"}:
                    assert after[table] == final[table]
        final = await snapshot(pg_sessions)
        row = final["webhook_notifications"][0]
        assert row["status"] == "completed" and row["last_error"] is None
        assert row["attempt_count"] == (1 if ending == "completed" else 2)
        for table in (
            "retrieval_receipts",
            "retrieval_events",
            "webhook_batch_items",
            "webhook_status_events",
        ):
            assert all(row in final[table] for row in before[table])
        assert final["webhook_events"][0]["raw_body"] == before["webhook_events"][0]["raw_body"]
        assert (await recovery.process_batch(batch)).completed_elements == 1
        redelivered = await snapshot(pg_sessions)
        for table in final.keys() - {"webhook_events"}:
            assert redelivered[table] == final[table]
    finally:
        for process, pipe in children:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 10)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 10)
            pipe.close()
        await blocker.aclose()


async def test_batch_summary_reads_outcomes_after_obtaining_its_write_lock(pg_sessions):
    runner = processor(pg_sessions, None)
    batch, digest = await ingest(runner)
    async with pg_sessions.begin() as session:
        row = await session.get(WebhookNotification, digest)
        row.status, row.last_error = "failed", "old attempt"
    blocked = asyncio.Event()
    engine = pg_sessions.kw["bind"].sync_engine

    def pending(conn, cursor, statement, parameters, context, many):
        if "FOR UPDATE" in statement or statement.startswith("UPDATE webhook_events"):
            blocked.set()

    task = None
    try:
        async with pg_sessions.begin() as blocker:
            await blocker.get(WebhookEvent, batch, with_for_update=True)
            event.listen(engine, "before_cursor_execute", pending)
            task = asyncio.create_task(runner._finalize_batch(batch))
            await asyncio.wait_for(blocked.wait(), 5)
            # A newer attempt commits while the older finalizer is queued.
            async with pg_sessions.begin() as session:
                row = await session.get(WebhookNotification, digest)
                row.status, row.last_error = "completed", None
        await asyncio.wait_for(task, 10)
        async with pg_sessions() as session:
            result = await session.get(WebhookEvent, batch)
            assert result.failure_count == 0 and result.last_error is None
            assert result.processed_at is not None
    finally:
        event.remove(engine, "before_cursor_execute", pending)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
