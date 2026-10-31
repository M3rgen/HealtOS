"""Independent runtimes must finish their own windows without sharing checkpoints."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from retrieval_records import END, START, Client, point
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sync_ownership_records import PausedClient, service, snapshot

from app.core.timeutil import as_utc
from app.db.base import Base
from app.google_health.client import InvalidPageTokenError, Page
from app.models.retrieval import RetrievalCheckpoint, RetrievalEvent, RetrievalReceipt
from app.models.state import JobStatus, SyncState
from app.services.retrieval import token_hash
from app.sync import ownership
from app.sync.incremental import STALE_RUNNING_AFTER


@pytest_asyncio.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ownership.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def independent(sessions):
    engine = create_async_engine(sessions.kw["bind"].url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def contention(monkeypatch):
    blocked, retry = asyncio.Event(), asyncio.Event()

    async def pause():
        # Hold only the first waiter; later contenders queue behind it as in production.
        if not blocked.is_set():
            blocked.set()
            await retry.wait()

    monkeypatch.setattr(ownership, "_contended", pause)
    return blocked, retry


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled", "reset"])
async def test_waiter_preserves_checkpoint_then_fetches_its_own_range(
    sessions, independent, contention, outcome
):
    record = point()
    pages = [Page([record], "p2"), Page([], None)]
    if outcome == "failed":
        pages[1] = RuntimeError("synthetic request failure")
    elif outcome == "reset":
        pages = [pages[0], InvalidPageTokenError("expired"), Page([record], "p3"), pages[1]]
    first_client = PausedClient(*pages, pause_at=3 if outcome == "reset" else 2)
    second_client = Client(*([Page([], None)] * (2 if outcome in {"failed", "cancelled"} else 1)))
    first, second = service(sessions, first_client), service(independent, second_client)
    blocked, retry = contention
    task = asyncio.create_task(first.run("heart-rate", START, END))
    waiter = None
    try:
        await asyncio.wait_for(first_client.entered.wait(), 5)
        # Slow Google request: heartbeat is old but the run still owns this type.
        async with sessions.begin() as session:
            state = await session.get(SyncState, "heart-rate")
            state.updated_at = datetime.now(UTC) - STALE_RUNNING_AFTER - timedelta(minutes=1)
        before = await snapshot(sessions)
        waiter = asyncio.create_task(second.run("heart-rate", END, END + timedelta(days=1)))
        await asyncio.wait_for(blocked.wait(), 5)
        assert second_client.tokens == [] and second.last_runs == {}
        assert await second.recover_interrupted() == 0
        assert await snapshot(sessions) == before
        state = before["sync_state"][0]
        pointer = before["retrieval_checkpoints"]
        assert as_utc(state["last_requested_from"]) == START
        assert state["last_page_token"] == (None if outcome == "reset" else "p2")
        assert (
            not pointer if outcome == "reset" else pointer[0]["next_token_hash"] == token_hash("p2")
        )

        if outcome == "cancelled":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            first_client.release.set()
            if outcome == "failed":
                with pytest.raises(RuntimeError, match="synthetic"):
                    await task
            else:
                # Finishing includes page commits and source reconciliation. Under
                # full-suite load it can exceed the event handshake's five seconds;
                # this test verifies ownership and durable state, not I/O latency.
                await asyncio.wait_for(task, 30)
        after_first = await snapshot(sessions)
        # The abandoned checkpoint remains an exact pointer to the committed page.
        if outcome in {"failed", "cancelled"}:
            assert after_first["sync_state"][0]["last_page_token"] == "p2"
            assert after_first["retrieval_checkpoints"] == before["retrieval_checkpoints"]
        retry.set()
        await asyncio.wait_for(waiter, 30)
        assert second_client.tokens == (
            ["p2", None] if outcome in {"failed", "cancelled"} else [None]
        )
        final = await snapshot(sessions)
        assert final["sync_state"][0]["status"] == JobStatus.completed
        assert as_utc(final["sync_state"][0]["last_requested_from"]) == END
        assert final["sync_state"][0]["last_page_token"] is None
        assert final["retrieval_checkpoints"][0]["next_token_hash"] is None
        assert [row["raw_json"] for row in final["raw_health_data"]] == [record]
        # History survives; a resumed receipt links only to the abandoned durable page.
        for table in ("retrieval_receipts", "retrieval_events", "raw_health_data_versions"):
            assert all(row in final[table] for row in after_first[table])
        async with sessions() as session:
            receipts = list(
                await session.scalars(
                    select(RetrievalReceipt).order_by(RetrievalReceipt.started_at)
                )
            )
            pointer = await session.get(RetrievalCheckpoint, ("sync", "heart-rate"))
            last_page = await session.get(RetrievalEvent, pointer.event_id)
        assert last_page.receipt_id == receipts[-1].id and receipts[-1].parent_event_id is None
        if outcome in {"failed", "cancelled"}:
            assert receipts[-2].parent_event_id == before["retrieval_checkpoints"][0]["event_id"]
        assert first_client.calls[0][1:3] == (START, END)
        assert second.last_runs["heart-rate"]["failed"] is False
    finally:
        for pending in (task, waiter):
            if pending is not None:
                pending.cancel()
        await asyncio.gather(*(t for t in (task, waiter) if t is not None), return_exceptions=True)


async def test_different_type_progresses_and_cancelled_waiter_changes_nothing(
    sessions, independent, contention
):
    first_client = PausedClient(Page([point()], "p2"), Page([], None))
    first = service(sessions, first_client)
    task = asyncio.create_task(first.run("heart-rate", START, END))
    waiter = None
    try:
        await asyncio.wait_for(first_client.entered.wait(), 5)
        other = service(independent, Client(Page([], None)))
        await asyncio.wait_for(other.run("steps", START, END), 5)
        before = await snapshot(sessions)
        second = service(independent, Client())
        waiter = asyncio.create_task(second.run("heart-rate", END, END + timedelta(days=1)))
        await asyncio.wait_for(contention[0].wait(), 5)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert await snapshot(sessions) == before
        assert second.last_runs == {} and second.client.tokens == []
        first_client.release.set()
        await asyncio.wait_for(task, 5)
        await asyncio.wait_for(second.run("heart-rate", END, END + timedelta(days=1)), 5)
        assert second.client.tokens == [None]
    finally:
        task.cancel()
        if waiter is not None:
            waiter.cancel()
        await asyncio.gather(*(t for t in (task, waiter) if t is not None), return_exceptions=True)


@pytest.mark.parametrize("lost_owner", ["run", "notification"])
async def test_owner_loss_never_falls_back_to_unprotected_state_writes(
    sessions, monkeypatch, lost_owner
):
    from contextlib import asynccontextmanager

    from app.sync import incremental

    lost = False

    class Guarded:
        @asynccontextmanager
        async def begin(self):
            if lost and lost_owner == "run":
                raise ownership.OwnershipLost("synthetic disconnect")
            async with sessions.begin() as session:
                yield session

    @asynccontextmanager
    async def owned(*args, **kwargs):
        yield Guarded()

    monkeypatch.setattr(incremental, "run_ownership", owned)
    client = PausedClient(Page([point()], "p2"), Page([point()], None))
    runner = service(sessions, client)

    async def check_owner():
        if lost and lost_owner == "notification":
            raise ownership.OwnershipLost("synthetic caller disconnect")

    task = asyncio.create_task(runner.run("heart-rate", START, END, check_owner=check_owner))
    try:
        await asyncio.wait_for(client.entered.wait(), 5)
        before = await snapshot(sessions)
        lost = True
        client.release.set()
        with pytest.raises(ownership.OwnershipLost):
            await task
        assert await snapshot(sessions) == before
        assert runner.last_runs["heart-rate"]["failed"] is True
        assert len(client.kept) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_notification_keys_leave_no_sidecar_file_or_lock_entry(
    sessions, independent, tmp_path
):
    for element in ("element-1", "element-2", "element-3"):
        name = ownership.NOTIFICATION_NAMESPACE
        async with ownership.run_ownership(sessions, element, namespace=name) as owned:
            assert owned is sessions
            # Another engine of this process still sees the notification as owned.
            async with ownership.run_ownership(
                independent, element, wait=False, namespace=name
            ) as busy:
                assert busy is None
    async with ownership.run_ownership(sessions, "heart-rate") as owned:
        assert owned is sessions
    # Only data types keep a cross-process sidecar; elements leave nothing behind.
    assert len(list(tmp_path.glob("*.lock"))) == 1
    assert ownership._local_locks == {}
