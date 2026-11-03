"""Page-budget pauses preserve the production archive and durable continuation chain."""

import json
import uuid

import httpx
import pytest
from raw_response_records import body, client, stored
from retrieval_records import END, START, Runner, point
from sqlalchemy import select
from test_retrieval_receipts import rows, status

from app.core.timeutil import as_utc
from app.google_health import pagination
from app.google_health.data_types import REGISTRY
from app.google_health.pagination import PaginationPaused
from app.models.state import BackfillJob, FetchRound, JobStatus, RawHealthData, SyncState
from app.services.storage import store_records
from app.sync.backfill import ABANDON_AFTER_RETRIES
from app.sync.ownership import run_ownership


@pytest.mark.parametrize("lane", ["sync", "backfill"])
@pytest.mark.parametrize("reset", [False, True])
@pytest.mark.parametrize("kind", ["steps", "floors", "total-calories"])
async def test_repeated_pauses_resume_committed_pages_and_complete(
    sessions, monkeypatch, lane, reset, kind
):
    monkeypatch.setattr(pagination, "MAX_PAGES_PER_CALL", 2)
    runner = Runner(sessions, lane, kind)
    missing = point(kind=kind)
    async with sessions.begin() as session:
        await store_records(session, REGISTRY[kind], [missing])
        session.add(SyncState(data_type=kind, last_successful_sync=START))
        if lane == "backfill":
            runner.job_id = uuid.uuid4()
            session.add(
                BackfillJob(
                    id=runner.job_id,
                    data_type=kind,
                    window_start=START,
                    window_end=END,
                    retry_count=ABANDON_AFTER_RETRIES - 1,
                )
            )
    tokens, wires, records = [], [], []
    expired = False
    terminal = False

    def handler(request):
        nonlocal expired
        params = json.loads(request.content) if request.method == "POST" else request.url.params
        token = params.get("pageToken")
        tokens.append(token)
        if reset and token == "p1" and not expired:
            expired = True
            wire = b'{"error":{"message":"INVALID_PAGE_TOKEN"}}'
            code = 400
        else:
            record = point(kind=kind)
            records.append(record)
            wire = body(record, token=None if terminal else f"p{len(records)}")
            wire = (
                wire.replace(b'"dataPoints":', b'"rollupDataPoints":')
                if kind == "total-calories"
                else wire
            )
            code = 200
        wires.append(wire)
        return httpx.Response(code, content=wire)

    google = client(handler, sessions)
    first_start = None
    async with google.http:
        for _ in range(3):
            before = len(records)
            with pytest.raises(PaginationPaused):
                await runner.run(google)
            assert len(records) - before == 2
            receipts, events = await rows(sessions)
            assert not any(e.kind in {"completed", "failed"} or e.last_page for e in events)
            assert (await status(sessions, runner))["retrieval"] == "incomplete"
            async with sessions() as session:
                state = await session.get(SyncState, kind)
                job = state if lane == "sync" else await session.get(BackfillJob, runner.job_id)
                owner = kind if lane == "sync" else str(runner.job_id)
                fetch = await session.get(FetchRound, (lane, owner))
                first_start = first_start or fetch.started_at
                assert fetch.started_at == first_start
                assert job.status == JobStatus.pending and job.last_error is None
                assert job.records_downloaded == len(records)
                assert as_utc(state.last_successful_sync) == START
                assert (
                    job.last_page_token if lane == "sync" else job.page_token
                ) == f"p{len(records)}"
                if lane == "sync":
                    assert (as_utc(job.last_requested_from), as_utc(job.last_requested_to)) == (
                        START,
                        END,
                    )
                else:
                    assert (as_utc(job.window_start), as_utc(job.window_end)) == (START, END)
                    assert job.retry_count == ABANDON_AFTER_RETRIES - 1 and job.completed_at is None
            async with run_ownership(sessions, kind, wait=False) as acquired:
                assert acquired is not None
            assert [r.body for r in await stored(sessions)] == wires
        terminal = True
        await runner.run(google)
    assert tokens == [None, "p1"] + ([None] if reset else []) + [f"p{i}" for i in range(2, 7)]
    receipts, events = await rows(sessions)
    assert [e.kind for e in events].count("completed") == 1
    assert receipts[-1].parent_event_id == [e for e in events if e.kind == "page"][-2].id
    assert (await status(sessions, runner))["retrieval"] == "completed_with_records"
    async with sessions() as session:
        raw = list(await session.scalars(select(RawHealthData)))
        assert {r.google_data_point_id for r in raw} == {r["name"] for r in [missing, *records]}
        assert all(r.source_deleted_at is None for r in raw)
        state = await session.get(SyncState, kind)
        job = state if lane == "sync" else await session.get(BackfillJob, runner.job_id)
        assert job.status == JobStatus.completed
        assert (job.last_page_token if lane == "sync" else job.page_token) is None
        assert as_utc(state.last_successful_sync) == (END if lane == "sync" else START)
    assert [r.body for r in await stored(sessions)] == wires
