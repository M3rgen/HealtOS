"""A page budget spent exactly at a boundary begins no further fetch or receipt."""

from datetime import timedelta

import pytest
from retrieval_records import END, START, Client, point
from sqlalchemy import select
from sync_ownership_records import service
from test_backfill_ownership import horizon as horizon
from test_backfill_ownership import runner

from app.core.timeutil import as_utc
from app.google_health import pagination
from app.google_health.client import Page
from app.google_health.pagination import PaginationPaused
from app.models.retrieval import RetrievalReceipt
from app.models.state import BackfillJob, FetchRound, JobStatus, SyncState

EARLIER = START - timedelta(days=1)


async def durable(sessions):
    async with sessions() as session:
        receipts = list(await session.scalars(select(RetrievalReceipt)))
        rounds = list(await session.scalars(select(FetchRound)))
        jobs = list(await session.scalars(select(BackfillJob).order_by(BackfillJob.window_start)))
        state = await session.get(SyncState, "heart-rate")
    return receipts, rounds, jobs, state


async def test_backfill_stops_before_selecting_a_window_it_cannot_fetch(sessions, monkeypatch):
    monkeypatch.setattr(pagination, "MAX_PAGES_PER_CALL", 1)
    async with sessions.begin() as session:
        for lower, upper in ((EARLIER, START), (START, END)):
            session.add(BackfillJob(data_type="heart-rate", window_start=lower, window_end=upper))
    client = Client(Page([point()], None))
    assert await runner(sessions, client).run(["heart-rate"]) == 1
    receipts, rounds, jobs, _ = await durable(sessions)
    assert [(as_utc(r.requested_from), r.lane) for r in receipts] == [(START, "backfill")]
    assert [r.key for r in rounds] == [str(jobs[1].id)]
    waiting, done = jobs
    assert done.status == JobStatus.completed
    assert waiting.status == JobStatus.pending and waiting.started_at is None


async def test_sync_records_the_requested_range_without_beginning_its_round(sessions, monkeypatch):
    monkeypatch.setattr(pagination, "MAX_PAGES_PER_CALL", 1)
    async with sessions.begin() as session:
        # A paused earlier caller's range is finished first and spends the budget.
        session.add(
            SyncState(
                data_type="heart-rate",
                status=JobStatus.pending,
                last_requested_from=EARLIER,
                last_requested_to=START,
            )
        )
    client = Client(Page([point()], None), Page([], None))
    sync = service(sessions, client)
    with pytest.raises(PaginationPaused):
        await sync.run("heart-rate", START, END)
    receipts, rounds, _, state = await durable(sessions)
    assert [as_utc(r.requested_from) for r in receipts] == [EARLIER]
    assert [r.kind for r in rounds] == ["sync"]
    assert state.status == JobStatus.pending and state.last_page_token is None
    assert (as_utc(state.last_requested_from), as_utc(state.last_requested_to)) == (START, END)
    # The next invocation fetches the pending range as its first round.
    await sync.run("heart-rate", START, END)
    receipts, rounds, _, state = await durable(sessions)
    assert [as_utc(r.requested_from) for r in receipts] == [EARLIER, START]
    assert client.tokens == [None, None]
    assert state.status == JobStatus.completed
