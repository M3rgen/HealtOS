"""Backfill process fencing on real PostgreSQL, including a stale owner's late reply."""

import asyncio
import multiprocessing
import zlib

import pytest
from retrieval_records import END, START
from sqlalchemy import text
from sync_ownership_records import snapshot
from test_pg_sync_ownership import receive, worker

from app.models.state import BackfillJob, JobStatus
from app.sync import ownership

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("ending", ["completed", "terminated", "disconnected"])
@pytest.mark.parametrize("waiter_lane", ["backfill", "sync"])
async def test_backfill_process_waiter_and_connection_loss(pg_sessions, ending, waiter_lane):
    async with pg_sessions.begin() as session:
        job = BackfillJob(data_type="heart-rate", window_start=START, window_end=END)
        session.add(job)
    url = pg_sessions.kw["bind"].url.render_as_string(hide_password=False)
    context = multiprocessing.get_context("spawn")
    children = []

    def start(kind, job_id=None):
        parent, child = context.Pipe()
        process = context.Process(target=worker, args=(url, child, kind, START, job_id))
        process.start()
        child.close()
        children.append((process, parent))
        return process, parent

    try:
        first, owner = start("heart-rate", job.id)
        assert await receive(owner) == ("fetch", START, END, None)
        owner.send("more")
        assert await receive(owner) == ("fetch", START, END, "p2")
        before = await snapshot(pg_sessions)
        _, waiter = start("heart-rate", job.id if waiter_lane == "backfill" else None)
        assert await receive(waiter) == ("busy",)
        assert await snapshot(pg_sessions) == before
        _, other = start("steps")
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
        assert transaction is None  # The slow Google call holds ownership, no transaction.
        if ending == "completed":
            owner.send("done")
            assert await receive(owner) == ("result", "completed")
        else:
            if ending == "terminated":
                first.terminate()
                await asyncio.to_thread(first.join, 10)
                assert not first.is_alive()
            async with pg_sessions.begin() as session:
                await session.execute(
                    text(
                        "SELECT pg_terminate_backend(:pid, 10000) "
                        "FROM pg_stat_activity WHERE pid = :pid"
                    ),
                    {"pid": pid},
                )
            interrupted = await snapshot(pg_sessions)
            assert interrupted["backfill_jobs"] == before["backfill_jobs"]
            # The independent steps sync committed its own checkpoint after `before`.
            backfill = [r for r in interrupted["retrieval_checkpoints"] if r["lane"] == "backfill"]
            assert backfill == before["retrieval_checkpoints"]
        waiter.send("retry")
        if waiter_lane == "sync" or ending != "completed":
            token = "p2" if waiter_lane == "backfill" else None
            assert await receive(waiter) == ("fetch", START, END, token)
            waiter.send("done")
        assert await receive(waiter) == ("result", "completed")
        final = await snapshot(pg_sessions)
        if ending == "disconnected":
            # A new owner has already committed. Only now let the old request finish.
            owner.send("done")
            result = await receive(owner)
            assert result[0] == "result" and result[1] != "completed"
            assert await snapshot(pg_sessions) == final
        backfill = final["backfill_jobs"][0]
        expected = (
            JobStatus.running
            if waiter_lane == "sync" and ending != "completed"
            else JobStatus.completed
        )
        assert backfill["status"] == expected and backfill["retry_count"] == 0
        assert backfill["records_downloaded"] == 1
        assert final["fetch_rounds"][0] in before["fetch_rounds"]
        for table in ("retrieval_receipts", "retrieval_events", "raw_health_data_versions"):
            assert all(row in final[table] for row in before[table])
        resumed = [r for r in final["retrieval_receipts"] if r["parent_event_id"] is not None]
        assert [r["parent_event_id"] for r in resumed] == (
            [before["retrieval_checkpoints"][0]["event_id"]]
            if waiter_lane == "backfill" and ending != "completed"
            else []
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
