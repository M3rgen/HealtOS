"""Rejected responses never commit a terminal page, including durable resumes."""

import asyncio
import hashlib
import json
from itertools import product

import httpx
import pytest
from raw_response_records import client, stored
from rejected_page_records import (
    ENCODINGS,
    KINDS,
    LANES,
    UNSTORABLE_TOKENS,
    client_rejects,
    rejected_page,
)
from retrieval_records import END, START, Client, Runner, point
from sqlalchemy import select

from app.core.timeutil import as_utc
from app.google_health.client import InvalidCollectionResponseError, Page
from app.google_health.data_types import REGISTRY
from app.google_health.pagination import RepeatedPageTokenError, iter_pages
from app.models.retrieval import RetrievalCheckpoint, RetrievalEvent
from app.models.state import BackfillJob, JobStatus, RawHealthData, SyncState
from app.services import storage
from app.services.retrieval_coverage import coverage_report
from app.sync import backfill, incremental
from app.sync.errors import error_summary


@pytest.fixture(params=LANES)
def lane(request):
    return request.param


LOCATIONS = [
    "https://example.invalid/private-marker",
    "https://example.invalid:bad/private-marker",
    "https://[invalid-ipv6]/private-marker",
]
# Location matters only to the client's status gate, whose own matrix covers every
# status and location; durable runs rotate the locations across these cases.
CASES = (
    [(200, t) for t in [False, True, 0, 1.5, [], {}, ["private-marker"], {"private-marker": 1}]]
    + [(s, t) for s in [301, 302, 303, 304, 307, 308] for t in [None, "records"]]
    + [
        (200, t)
        for t in product(["deflate", "raw-deflate"], ["junk", "zero", "stream"], [None, "records"])
    ]
)


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("first_page", [False, True])
@pytest.mark.parametrize("case", range(len(CASES)))
async def test_rejected_page_and_resume_preserve_progress_and_archive(
    sessions, caplog, lane, first_page, case
):
    status, token = CASES[case]
    await rejected_page(sessions, lane, first_page, status, token, LOCATIONS[case % 3], caplog)


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("first_page", [False, True])
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("token", UNSTORABLE_TOKENS)
async def test_unstorable_token_preserves_progress(sessions, caplog, lane, first_page, kind, token):
    caplog.set_level("INFO")
    await rejected_page(sessions, lane, first_page, 200, token, None, caplog, kind)


VALUES = [None, [], [{"private-marker": "HTTP 503 INVALID_PAGE_TOKEN"}]]
CONTINUATIONS = [{}, {"nextPageToken": "next"}]


def wrong_collection(kind, value, continuation):
    other = "dataPoints" if kind == "total-calories" else "rollupDataPoints"
    return ("  " + json.dumps({other: value, **continuation}) + "\n").encode()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize("continuation", CONTINUATIONS)
async def test_client_rejects_wrong_collection(kind, value, continuation, caplog):
    raw = wrong_collection(kind, value, continuation)
    await client_rejects(kind, raw, InvalidCollectionResponseError, caplog)


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("first_page", [False, True])
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("case", range(4))
async def test_wrong_collection_preserves_progress_and_archive(
    sessions, caplog, lane, first_page, kind, case
):
    # The client matrix covers every value, continuation and encoding.
    raw = wrong_collection(kind, VALUES[case % 3], CONTINUATIONS[case % 2])
    response = (raw, ENCODINGS[case], InvalidCollectionResponseError)
    await rejected_page(sessions, lane, first_page, 200, None, None, caplog, kind, response)


@pytest.mark.parametrize("kind", KINDS)
async def test_rejection_keeps_retries_when_archiving_is_cancelled(sessions, kind):
    from app.services.responses import DatabaseResponseArchive

    started, release, finished = (asyncio.Event() for _ in range(3))

    class SlowArchive(DatabaseResponseArchive):
        async def keep(self, responses):
            started.set()
            await release.wait()
            await super().keep(responses)
            finished.set()

    other = "dataPoints" if kind == "total-calories" else "rollupDataPoints"
    bad = json.dumps({other: None}).encode()
    exchanges = iter([(503, b"retry"), (200, bad)])

    def handler(_):
        status, wire = next(exchanges)
        return httpx.Response(status, stream=httpx.ByteStream(wire))

    google = client(handler, google_api_max_retries=1)
    google.archive = SlowArchive(sessions)
    async with google.http:
        pages = iter_pages(google, REGISTRY[kind], START, END, "saved")
        task = asyncio.create_task(anext(pages))
        try:
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            assert finished.is_set()
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    rows = await stored(sessions)
    assert [r.body for r in rows] == [b"retry", bad]
    assert [r.attempt for r in rows] == [0, 1]
    assert all(r.complete and r.body_sha256 == hashlib.sha256(r.body).hexdigest() for r in rows)


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("repeated", ["self", "cycle"])
async def test_cycle_preserves_progress_and_resume(sessions, caplog, lane, kind, repeated):
    caplog.set_level("INFO", logger="app")
    await rejected_page(sessions, lane, False, 200, None, None, caplog, kind, repeated=repeated)


async def test_cyclic_page_is_stored_without_advancing_checkpoints(sessions, lane):
    runner = Runner(sessions, lane)
    first, cyclic = point(), point("2026-09-01T15:00:00Z")
    async with sessions.begin() as session:
        session.add(SyncState(data_type=runner.kind, last_successful_sync=START))

    async def snapshot():
        async with sessions() as session:
            state = await session.get(SyncState, runner.kind)
            job = state if lane == "sync" else await session.get(BackfillJob, runner.job_id)
            raw = list(await session.scalars(select(RawHealthData)))
            checkpoint = await session.scalar(select(RetrievalCheckpoint))
            events = list(await session.scalars(select(RetrievalEvent).order_by(RetrievalEvent.id)))
        token = job.last_page_token if lane == "sync" else job.page_token
        return state, job, token, raw, checkpoint, events

    # The second run fetches the same cyclic page again; Google repairs the third.
    for attempt, pages in enumerate(
        [[Page([first], "repeat"), Page([cyclic], "repeat")], [Page([cyclic], "repeat")]], 1
    ):
        google = Client(*pages)
        with pytest.raises(RepeatedPageTokenError) as caught:
            await runner.run(google)
        assert error_summary(caught.value) == "RepeatedPageTokenError (repeated_page_token)"
        assert google.tokens == [None, "repeat"][-len(pages) :] and google.kept == []
        state, job, token, raw, checkpoint, events = await snapshot()
        assert job.status == JobStatus.failed and token == "repeat"
        assert job.records_downloaded == 1 + attempt
        assert as_utc(state.last_successful_sync) == START
        payloads = {r.google_data_point_id: r.raw_json for r in raw}
        assert len(raw) == 2 and payloads == {first["name"]: first, cyclic["name"]: cyclic}
        assert [e.kind for e in events] == ["page"] + ["failed"] * attempt
        assert [e.returned_count for e in events] == [1] * (1 + attempt)
        assert checkpoint.event_id == events[0].id
        assert checkpoint.next_token_hash == hashlib.sha256(b"repeat").hexdigest()
    await runner.run(Client(Page([cyclic], None)))
    state, job, token, raw, _, events = await snapshot()
    assert job.status == JobStatus.completed and token is None and len(raw) == 2
    assert [e.kind for e in events][-2:] == ["page", "completed"]
    assert as_utc(state.last_successful_sync) == (END if lane == "sync" else START)
    # The checkpoint never pointed at the cyclic page, so the repaired chain proves the day.
    async with sessions() as session:
        report = await coverage_report(
            session, REGISTRY, START.date(), START.date(), "UTC", runner.kind
        )
    assert report["types"][0]["days"][0]["retrieval"] == "completed_with_records"


async def test_failed_store_of_cyclic_page_archives_it_once(sessions, lane, monkeypatch):
    stored_page = storage.store_page

    async def store(session, spec, page, **kwargs):
        if page.cyclic:
            raise RuntimeError("synthetic store failure")
        return await stored_page(session, spec, page, **kwargs)

    monkeypatch.setattr(incremental, "store_page", store)
    monkeypatch.setattr(backfill, "store_page", store)
    google = Client(Page([point()], "repeat"), Page([point()], "repeat"))
    with pytest.raises(RuntimeError, match="synthetic store failure") as caught:
        await Runner(sessions, lane).run(google)
    assert google.kept == [((), caught.value)]
    async with sessions() as session:
        assert len(list(await session.scalars(select(RawHealthData)))) == 1
