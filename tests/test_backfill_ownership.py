"""Controlled Google waits with independent engines sharing one durable archive."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest
from retrieval_records import END, START, Client, point
from sqlalchemy import select
from sync_ownership_records import PausedClient, service, snapshot
from test_sync_ownership import contention as contention
from test_sync_ownership import independent as independent
from test_sync_ownership import sessions as sessions

from app.core.config import Settings
from app.core.timeutil import as_utc
from app.google_health.client import InvalidPageTokenError, Page
from app.google_health.data_types import REGISTRY
from app.models.state import BackfillJob, JobStatus
from app.services.retrieval import token_hash
from app.sync import backfill, ownership


def runner(sessions, client):
    return backfill.BackfillService(
        Settings(_env_file=None, backfill_start_date=START.date(), backfill_window_days=1),
        sessions,
        client,
        dict(REGISTRY),
    )


@pytest.fixture(autouse=True)
def horizon(monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return END

    monkeypatch.setattr(backfill, "datetime", Frozen)


async def add_job(sessions, status=JobStatus.pending):
    async with sessions.begin() as session:
        job = BackfillJob(data_type="heart-rate", window_start=START, window_end=END, status=status)
        session.add(job)
    return job.id


@pytest.mark.parametrize("status", [JobStatus.pending, JobStatus.failed, JobStatus.running])
@pytest.mark.parametrize("ending", ["completed", "failed", "cancelled", "reset", "rollback"])
async def test_waiter_rechecks_jobs_and_only_resumes_durable_pages(
    sessions, independent, contention, monkeypatch, status, ending
):
    record = point()
    pages = [Page([record], "p2"), Page([record], None)]
    if ending == "failed":
        pages[1] = RuntimeError("request failed")
    elif ending == "reset":
        pages = [pages[0], InvalidPageTokenError("expired"), Page([], None)]
    job_id = await add_job(sessions, status)
    client = PausedClient(*pages)
    first, second = runner(sessions, client), runner(independent, Client(Page([], None)))
    if ending == "rollback":
        original = backfill.store_page

        async def store(session, spec, page, **kwargs):
            await original(session, spec, page, **kwargs)
            if page is pages[1]:
                raise RuntimeError("page rollback")

        monkeypatch.setattr(backfill, "store_page", store)
    task = asyncio.create_task(first.run(["heart-rate"]))
    waiter = None
    try:
        await asyncio.wait_for(client.entered.wait(), 5)
        before = await snapshot(sessions)
        waiter = asyncio.create_task(second.run(["heart-rate"]))
        await asyncio.wait_for(contention[0].wait(), 5)
        assert second.client.tokens == [] and await snapshot(sessions) == before
        if ending == "cancelled":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            client.release.set()
            assert await asyncio.wait_for(task, 10) == int(ending in {"completed", "reset"})
        partial = await snapshot(sessions)
        resumed = ending in {"failed", "cancelled", "rollback"}
        if resumed:
            assert partial["backfill_jobs"][0]["page_token"] == "p2"
            assert partial["retrieval_checkpoints"] == before["retrieval_checkpoints"]
            assert partial["fetch_rounds"] == before["fetch_rounds"]
        contention[1].set()
        assert await asyncio.wait_for(waiter, 10) == int(resumed)
        assert second.client.tokens == (["p2"] if resumed else [])
        final = await snapshot(sessions)
        job = final["backfill_jobs"][0]
        assert job["status"] == JobStatus.completed and job["page_token"] is None
        assert job["records_downloaded"] == (2 if ending == "completed" else 1)
        assert job["retry_count"] == int(ending in {"failed", "rollback"})
        assert final["raw_health_data"][0]["raw_json"] == record
        pointer = final["retrieval_checkpoints"][0]
        assert pointer["owner"] == str(job_id) and pointer["next_token_hash"] is None
        if resumed:
            child = next(r for r in final["retrieval_receipts"] if r["parent_event_id"] is not None)
            assert child["parent_event_id"] == before["retrieval_checkpoints"][0]["event_id"]
            assert child["resume_token_hash"] == token_hash("p2")
        for table in ("retrieval_receipts", "retrieval_events", "raw_health_data_versions"):
            assert all(row in final[table] for row in partial[table])
    finally:
        for pending in (task, waiter):
            if pending is not None:
                pending.cancel()
        await asyncio.gather(*(t for t in (task, waiter) if t is not None), return_exceptions=True)


class Empty(Client):
    async def list_data_points(self, spec, start, end, page_token=None):
        self.tokens.append((spec.name, page_token))
        return Page([], None)


@pytest.mark.parametrize("mode", ["empty", "extend", "catalog"])
async def test_concurrent_planners_read_boundaries_after_waiting(
    sessions, independent, contention, mode
):
    first, second = runner(sessions, Empty()), runner(independent, Empty())
    catalog = mode == "catalog"
    kind = "food" if catalog else "heart-rate"
    if mode == "extend":
        async with sessions.begin() as session:
            session.add(
                BackfillJob(
                    data_type=kind,
                    window_start=START - timedelta(days=1),
                    window_end=START,
                    status=JobStatus.completed,
                )
            )
    first.settings.backfill_start_date = (START - timedelta(days=2)).date()
    second.settings.backfill_start_date = first.settings.backfill_start_date
    entered, release = asyncio.Event(), asyncio.Event()
    original = first._ensure_jobs

    async def plan(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    first._ensure_jobs = plan
    task = asyncio.create_task(first.run([kind]))
    waiter = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        waiter = asyncio.create_task(second.run([kind]))
        await asyncio.wait_for(contention[0].wait(), 5)
        release.set()
        contention[1].set()
        # Windows interleave between the two runs, but each planned window runs once.
        done = await asyncio.wait_for(task, 5) + await asyncio.wait_for(waiter, 5)
        assert done == (1 if catalog else 2 if mode == "extend" else 3)
        assert len(first.client.tokens + second.client.tokens) == done
        async with sessions() as session:
            jobs = list(
                await session.scalars(select(BackfillJob).order_by(BackfillJob.window_start))
            )
        assert len(jobs) == (1 if catalog else 3)
        assert as_utc(jobs[0].window_start) == START - timedelta(days=2)
        assert as_utc(jobs[-1].window_end) == END
        assert all(a.window_end == b.window_start for a, b in zip(jobs, jobs[1:], strict=False))
    finally:
        task.cancel()
        if waiter is not None:
            waiter.cancel()
        await asyncio.gather(*(t for t in (task, waiter) if t is not None), return_exceptions=True)


@pytest.mark.parametrize("backfill_first", [False, True])
async def test_incremental_shares_owner_other_type_progresses_and_waiter_cancels(
    sessions, independent, contention, backfill_first
):
    class MixedClient(PausedClient):
        async def list_data_points(self, spec, *args, **kwargs):
            if spec.name == "steps":
                return Page([], None)
            return await super().list_data_points(spec, *args, **kwargs)

    paused = MixedClient(Page([], None), pause_at=1)
    first = runner(sessions, paused) if backfill_first else service(sessions, paused)
    second = service(independent, Client()) if backfill_first else runner(independent, Client())

    async def invoke(instance, kind):
        if isinstance(instance, backfill.BackfillService):
            return await instance.run([kind])
        return await instance.run(kind, START, END)

    task = asyncio.create_task(invoke(first, "heart-rate"))
    waiter = None
    try:
        await asyncio.wait_for(paused.entered.wait(), 5)
        # The same instance must permit a different type during a slow Google call.
        await asyncio.wait_for(invoke(first, "steps"), 5)
        before = await snapshot(sessions)
        waiter = asyncio.create_task(invoke(second, "heart-rate"))
        await asyncio.wait_for(contention[0].wait(), 5)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert second.client.tokens == [] and await snapshot(sessions) == before
        paused.release.set()
        await asyncio.wait_for(task, 5)
        await asyncio.wait_for(invoke(second, "heart-rate"), 5)
        assert second.client.tokens == [None]
    finally:
        task.cancel()
        if waiter is not None:
            waiter.cancel()
        await asyncio.gather(*(t for t in (task, waiter) if t is not None), return_exceptions=True)


@pytest.mark.parametrize("reset", [False, True])
async def test_lost_owner_cannot_change_replacements_success(
    sessions, independent, monkeypatch, reset
):
    lost = False

    class Guarded:
        @asynccontextmanager
        async def begin(self):
            if lost:
                raise ownership.OwnershipLost("synthetic connection loss")
            async with sessions.begin() as session:
                yield session

    @asynccontextmanager
    async def owned(*args, **kwargs):
        yield Guarded()

    await add_job(sessions)
    client = PausedClient(
        Page([point()], "p2"), InvalidPageTokenError("expired") if reset else Page([point()], None)
    )
    original = backfill.run_ownership
    monkeypatch.setattr(backfill, "run_ownership", owned)
    task = asyncio.create_task(runner(sessions, client).run(["heart-rate"]))
    try:
        await asyncio.wait_for(client.entered.wait(), 5)
        before = await snapshot(sessions)
        lost = True
        monkeypatch.setattr(backfill, "run_ownership", original)
        replacement = runner(independent, Client(Page([], None)))
        assert await replacement.run(["heart-rate"]) == 1
        assert replacement.client.tokens == ["p2"]
        completed = await snapshot(sessions)
        client.release.set()
        # The lost owner's window ends without any write; the type pauses.
        assert await task == 0
        assert await snapshot(sessions) == completed
        assert completed["fetch_rounds"] == before["fetch_rounds"]
        assert len(client.kept) == int(not reset)
        # Terminal jobs are never fetched by a later run either.
        assert await replacement.run(["heart-rate"]) == 0
        assert await snapshot(sessions) == completed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_waiting_incremental_runs_between_backfill_windows(
    sessions, independent, monkeypatch
):
    calls = []

    class Logged(PausedClient):
        async def list_data_points(self, spec, start, end, page_token=None):
            calls.append(("backfill", as_utc(start)))
            return await super().list_data_points(spec, start, end, page_token)

    class Recent(Client):
        async def list_data_points(self, spec, start, end, page_token=None):
            calls.append(("sync", as_utc(start)))
            return Page([], None)

    async with sessions.begin() as session:
        for lower in (START - timedelta(days=1), START):
            session.add(
                BackfillJob(
                    data_type="heart-rate", window_start=lower, window_end=lower + (END - START)
                )
            )
    queued = asyncio.Event()

    async def contended():
        queued.set()

    monkeypatch.setattr(ownership, "_contended", contended)
    client = Logged(Page([], None), Page([], None), pause_at=1)
    task = asyncio.create_task(runner(sessions, client).run(["heart-rate"]))
    try:
        await asyncio.wait_for(client.entered.wait(), 5)
        sync = asyncio.create_task(service(independent, Recent()).run("heart-rate", START, END))
        await asyncio.wait_for(queued.wait(), 5)
        client.release.set()
        assert await asyncio.wait_for(task, 5) == 2
        await asyncio.wait_for(sync, 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    # The sync waited for one window, not for the type's whole history.
    assert calls == [("backfill", START), ("sync", START), ("backfill", START - timedelta(days=1))]


async def test_type_waiting_for_its_owner_leaves_the_fetch_slot_to_others(sessions, independent):
    client = Empty()
    service = runner(sessions, client)  # SQLite allows one fetch slot
    async with ownership.run_ownership(independent, "heart-rate"):
        # Listed first, the busy type must not hold the only slot while it waits.
        task = asyncio.create_task(service.run(["heart-rate", "steps"]))
        for _ in range(100):
            if client.tokens:
                break
            await asyncio.sleep(0.01)
        assert client.tokens == [("steps", None)]
    assert await asyncio.wait_for(task, 5) == 2
    assert client.tokens == [("steps", None), ("heart-rate", None)]
