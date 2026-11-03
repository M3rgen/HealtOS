"""Retry-After failures preserve durable progress and release sync/backfill ownership."""

import asyncio
import hashlib
import json

import httpx
import pytest
from raw_response_records import client, stored
from retrieval_records import START, Runner, point
from sqlalchemy import select

from app.core.timeutil import as_utc
from app.google_health.client import GoogleHealthError
from app.google_health.data_types import REGISTRY
from app.models.retrieval import RetrievalCheckpoint, RetrievalEvent
from app.models.state import BackfillJob, JobStatus, RawHealthData, SyncState
from app.services.storage import store_records
from app.sync.errors import error_summary, is_transient


@pytest.mark.parametrize("lane", ["sync", "backfill"])
@pytest.mark.parametrize("status", [429, 500, 503, 599])
@pytest.mark.parametrize(
    "header", ["60", "60.001", "999999999999", "Fri, 31 Dec 9999 23:59:59 GMT"]
)
async def test_retry_after_preserves_checkpoint_and_allows_resume(sessions, lane, status, header):
    runner = Runner(sessions, lane, kind="steps")
    old, record = point(kind="steps"), point(kind="steps")
    good = json.dumps({"dataPoints": [record], "nextPageToken": "saved"}).encode()
    wires = [good, b" retry \n", b" rejected \n"]
    calls, sleeps = [], []

    def handler(request):
        calls.append(request.url.params.get("pageToken"))
        n = len(calls)
        if n > 3:
            return httpx.Response(200, json={})
        return httpx.Response(
            200 if n == 1 else status,
            content=wires[n - 1],
            headers={"Retry-After": "0" if n == 2 else header},
        )

    async def sleep(delay):
        sleeps.append(delay)
        if delay:
            # Cancel at the retry boundary, independently of database/runner speed.
            # An excessive Retry-After must fail before reaching this sleep.
            assert header == "60" and delay == 60
            task.cancel()
            await asyncio.sleep(0)

    async with sessions.begin() as session:
        await store_records(session, REGISTRY["steps"], [old])
        session.add(SyncState(data_type="steps", last_successful_sync=START))
    google = client(handler, sessions, google_api_max_retries=2)
    google.sleep = sleep
    task = asyncio.create_task(runner.run(google))
    try:
        with pytest.raises(asyncio.CancelledError if header == "60" else GoogleHealthError) as err:
            await asyncio.wait_for(task, 60)  # deadlock guard, not a speed limit
        if header != "60":
            assert is_transient(err.value)
            assert error_summary(err.value) == f"RetryDeferredError (http_{status})"
            assert str(err.value) == f"Google Health API retry deferred (HTTP {status})"
        assert calls == [None, "saved", "saved"]
        assert sleeps == ([0, 60] if header == "60" else [0])
        async with sessions() as session:
            state = await session.get(SyncState, "steps")
            job = state if lane == "sync" else await session.get(BackfillJob, runner.job_id)
            checkpoint = await session.scalar(select(RetrievalCheckpoint))
            events = list(await session.scalars(select(RetrievalEvent).order_by(RetrievalEvent.id)))
            raw = list(await session.scalars(select(RawHealthData)))
        assert as_utc(state.last_successful_sync) == START
        assert (job.last_page_token if lane == "sync" else job.page_token) == "saved"
        assert job.records_downloaded == 1 and job.status != JobStatus.completed
        if header != "60":
            assert job.last_error == error_summary(err.value)
        assert [e.kind for e in events] == ["page", "cancelled" if header == "60" else "failed"]
        assert not any(e.last_page for e in events) and checkpoint.event_id == events[0].id
        assert {r.google_data_point_id for r in raw} == {old["name"], record["name"]}
        assert all(r.source_deleted_at is None for r in raw)
        rows = await stored(sessions)
        assert [r.body for r in rows] == wires
        assert [r.attempt for r in rows] == [0, 0, 1]
        assert rows[-1].headers["retry-after"] == header
        assert all(r.complete and r.body_sha256 == hashlib.sha256(r.body).hexdigest() for r in rows)
        await asyncio.wait_for(runner.run(google), 60)
        assert calls[-1] == "saved"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await google.http.aclose()
