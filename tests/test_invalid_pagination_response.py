"""Rejected responses never commit a terminal page, including durable resumes."""

import asyncio
import hashlib
import json
from itertools import product

import httpx
import pytest
from raw_response_records import client, stored
from rejected_page_records import ENCODINGS, KINDS, LANES, client_rejects, rejected_page
from retrieval_records import END, START

from app.google_health.client import InvalidCollectionResponseError
from app.google_health.data_types import REGISTRY

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
async def test_wrong_collection_keeps_retries_when_archiving_is_cancelled(sessions, kind):
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
        task = asyncio.create_task(google.list_data_points(REGISTRY[kind], START, END))
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
