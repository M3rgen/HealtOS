"""Independent SQLite connections cover the complete notification ownership lifetime."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace

import pytest
import test_sync_ownership as sqlite
from retrieval_records import START, Client, point
from sync_ownership_records import PausedClient
from webhook_ownership_records import age, ingest, processor, snapshot

from app.google_health.client import Page
from app.services import webhook_processing
from app.sync.ownership import NOTIFICATION_NAMESPACE, OwnershipLost, run_ownership

sessions, independent, contention = sqlite.sessions, sqlite.independent, sqlite.contention


@pytest.fixture(params=["google", "type", "gap"])
async def paused(request, sessions, independent, contention):
    phase = request.param
    entered, resume = asyncio.Event(), asyncio.Event()
    client = PausedClient(
        Page([point()], None), Page([], None), pause_at=1 if phase == "google" else 99
    )
    first, second = (
        processor(sessions, client),
        processor(independent, Client(Page([], None), Page([], None))),
    )
    batch, digest = await ingest(first, windows=2 if phase == "gap" else 1)
    original = first.incremental.run

    async def between_windows(*args, **kwargs):
        result = await original(*args, **kwargs)
        if len(client.tokens) == 1:
            entered.set()
            await resume.wait()
        return result

    if phase == "gap":
        first.incremental.run = between_windows
    blocker = AsyncExitStack()
    if phase == "type":
        await blocker.enter_async_context(run_ownership(independent, "heart-rate"))
        entered = contention[0]
    elif phase == "google":
        entered = client.entered

    async def release():
        await blocker.aclose()
        contention[1].set()
        client.release.set()
        resume.set()

    task = asyncio.create_task(first.process_batch(batch))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        yield SimpleNamespace(
            second=second,
            task=task,
            phase=phase,
            release=release,
            batch=batch,
            digest=digest,
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await release()


async def test_live_owner_survives_recovery_replay_and_redelivery(paused, sessions):
    p = paused
    await age(sessions)
    before = await snapshot(sessions)
    assert await p.second.recover_running() == 0
    assert (await p.second.process_pending()).completed_elements == 0
    assert await snapshot(sessions) == before
    assert not p.second.incremental.client.tokens
    # Another notification progresses even in the gap where it needs the SAME type.
    other = processor(p.second.sessions, Client(Page([], None)))
    batch, _ = await ingest(
        other, kind="heart-rate" if p.phase == "gap" else "steps", start=START.replace(day=3)
    )
    assert (await asyncio.wait_for(other.process_batch(batch), 5)).completed_elements == 1
    await p.release()
    assert (await asyncio.wait_for(p.task, 10)).completed_elements == 1
    final = await snapshot(sessions)
    row = next(r for r in final["webhook_notifications"] if r["element_hash"] == p.digest)
    windows = 2 if p.phase == "gap" else 1
    assert row["status"] == "completed" and row["attempt_count"] == 1
    assert (row["sync_attempt_count"], row["sync_success_count"]) == (windows, windows)
    await p.second.process_batch(p.batch)
    assert not p.second.incremental.client.tokens
    for table in ("webhook_batch_items", "raw_health_data_versions", "retrieval_events"):
        assert all(row in final[table] for row in before[table])
    raw_before = before["webhook_events"][0]
    raw_after = next(r for r in final["webhook_events"] if r["payload_hash"] == p.batch)
    for name in ("raw_json", "raw_body"):
        assert raw_after[name] == raw_before[name]
    assert all(r in final["webhook_status_events"] for r in before["webhook_status_events"])
    assert {r["reason"] for r in final["webhook_status_events"]} == {"notification_received"}


async def test_cancelled_owner_is_recovered_and_retried(paused, sessions):
    p = paused
    p.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await p.task
    await p.release()
    await age(sessions)
    before = await snapshot(sessions)
    assert await p.second.recover_running() == 1
    assert await p.second.recover_running() == 0
    assert (await p.second.process_pending()).completed_elements == 1
    final = await snapshot(sessions)
    row = final["webhook_notifications"][0]
    assert row["status"] == "completed" and row["attempt_count"] == 2
    assert row["last_error"] is None and row["processed_at"] is not None
    assert len(p.second.incremental.client.tokens) == (2 if p.phase == "gap" else 1)
    for table in ("raw_health_data_versions", "retrieval_events", "webhook_status_events"):
        assert all(row in final[table] for row in before[table])
    assert [r["reason"] for r in final["webhook_status_events"]] == [
        "notification_received",
        "processing_interrupted",
    ]


@pytest.mark.parametrize("late", ["success", "error"])
async def test_lost_owner_cannot_overwrite_replacement(sessions, independent, monkeypatch, late):
    # Simulate a backend loss offline; the PostgreSQL process test kills real backends.
    lost = False
    actual = webhook_processing.run_ownership

    class Guarded:
        @asynccontextmanager
        async def begin(self):
            if lost:
                raise OwnershipLost("synthetic lost notification connection")
            async with sessions.begin() as session:
                yield session

    @asynccontextmanager
    async def owned(factory, key, **kwargs):
        if factory is sessions and kwargs.get("namespace") == NOTIFICATION_NAMESPACE:
            yield Guarded()
        else:
            async with actual(factory, key, **kwargs) as acquired:
                yield acquired

    monkeypatch.setattr(webhook_processing, "run_ownership", owned)
    entered, release = asyncio.Event(), asyncio.Event()
    first, second = processor(sessions, None), processor(independent, Client(Page([], None)))

    async def late_reply(*args, **kwargs):
        entered.set()
        await release.wait()
        if late == "error":
            raise RuntimeError("late Google failure")

    first.incremental.run = late_reply
    batch, _ = await ingest(first)
    task = asyncio.create_task(first.process_batch(batch))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        lost = True
        await age(sessions)
        assert await second.recover_running() == 1
        assert (await second.process_pending()).completed_elements == 1
        final = await snapshot(sessions)
        release.set()
        with pytest.raises(OwnershipLost):
            await task
        after = await snapshot(sessions)
        for table in ("webhook_notifications", "webhook_events", "webhook_batch_items"):
            assert after[table] == final[table]
        row = final["webhook_notifications"][0]
        assert row["status"] == "completed" and row["attempt_count"] == 2
        assert (row["sync_attempt_count"], row["sync_success_count"]) == (2, 1)
        assert row["last_error"] is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
