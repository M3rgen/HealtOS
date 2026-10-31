"""One durable-progress scenario shared by every page rejection family.

The client-level matrices check each rejected body and encoding without a database;
this scenario checks, per lane, that a rejected page keeps progress and the archive.
"""

import hashlib
import json

import httpx
import pytest
from compressed_records import compressed
from raw_response_records import FlowingBody, client, stored
from retrieval_records import END, START, Runner, point
from sqlalchemy import select

from app.core.timeutil import as_utc
from app.google_health.client import GoogleHealthError
from app.google_health.data_types import REGISTRY
from app.models.retrieval import RetrievalCheckpoint, RetrievalEvent, RetrievalReceipt
from app.models.state import BackfillJob, JobStatus, RawHealthData, SyncState
from app.services.storage import store_records
from app.sync.errors import error_summary, is_transient

LANES = ["sync", "backfill"]
KINDS = ["steps", "floors", "total-calories"]
ENCODINGS = ["identity", "gzip", "deflate", "raw-deflate"]


async def rejected_page(
    sessions, lane, first_page, status, token, location, caplog, kind="steps", rejection=None
):
    """Reject one page twice in a lane, then resume from the saved page and complete.

    ``rejection`` is ``(raw, encoding, error_type)`` for a 200 body the client rejects.
    """
    compression, token = (token[:2], token[2]) if isinstance(token, tuple) else (None, token)
    runner = Runner(sessions, lane, kind=kind)
    old, first, rejected = [point(kind="steps") for _ in range(3)]
    for record in (old, first, rejected):
        record["name"] = record["name"].replace("/steps/", f"/{kind}/")
        fields = record.pop("steps")
        fields["kcal" if kind == "total-calories" else "count"] = fields.pop("count")
        record[REGISTRY[kind].json_key] = fields
    rejected["name"] += "/private-marker"
    async with sessions.begin() as session:
        await store_records(session, REGISTRY[kind], [old])
        session.add(SyncState(data_type=kind, last_successful_sync=START))
    key = "rollupDataPoints" if kind == "total-calories" else "dataPoints"
    good = json.dumps({key: [first], "nextPageToken": "saved"}).encode()
    payload = {
        key: [] if compression and token is None else [rejected],
        "nextPageToken": None if status == 206 else token,
    }
    bad = ("  " + json.dumps(payload) + "\n").encode()
    if status != 200:
        bad = (
            ("  " + json.dumps({key: [], "private-marker": "synthetic"}) + "\n").encode()
            if token is None
            else bad
        )
    prefix = [] if first_page else [(200, good)]
    if rejection is not None:
        raw, encoding, error_type = rejection
        bad = compressed(raw, encoding)
    if compression:
        bad = compressed(bad, *compression)
    exchanges = iter(prefix + [(503, b"retry"), (status, bad)] * 2 + [(200, b"{}")])
    calls, streams = [], []

    def handler(request):
        params = json.loads(request.content) if request.method == "POST" else request.url.params
        calls.append(params.get("pageToken"))
        status, wire = next(exchanges)
        header = "Content-Range" if status == 206 else "Location"
        stream = FlowingBody(wire, finish=True)
        stream.release.set()
        streams.append(stream)
        headers = {header: location} if location else {}
        if compression and wire is bad:
            headers["content-encoding"] = compression[0].removeprefix("raw-")
        if rejection is not None and wire is bad:
            headers["content-encoding"] = encoding.removeprefix("raw-")
        return httpx.Response(status, stream=stream, headers=headers)

    google = client(handler, sessions, google_api_max_retries=1)
    google.http.follow_redirects = True
    saved = None if first_page else "saved"
    summary = f"GoogleHealthError (http_{status})"
    message = f"Google Health API returned HTTP {status}"
    if status == 200:
        summary = "InvalidPaginationResponseError (invalid_response)"
        message = "Google Health response has an invalid pagination token type"
    if compression:  # Bytes after the deflate stream: deterministic, never retried.
        summary = "GoogleHealthError (invalid_encoding)"
        message = "Google Health response has trailing bytes after its encoded content"
    if rejection is not None:
        summary = error_summary(error_type())
        message = str(error_type())
    try:
        for attempt in (1, 2):
            with pytest.raises(GoogleHealthError) as caught:
                await runner.run(google)
            assert str(caught.value) == message
            assert caught.value.__cause__ is caught.value.__context__ is None
            async with sessions() as session:
                state = await session.get(SyncState, kind)
                job = state if lane == "sync" else await session.get(BackfillJob, runner.job_id)
                checkpoint = await session.scalar(select(RetrievalCheckpoint))
                events = list(
                    await session.scalars(select(RetrievalEvent).order_by(RetrievalEvent.id))
                )
                receipts = list(
                    await session.scalars(
                        select(RetrievalReceipt).order_by(RetrievalReceipt.started_at)
                    )
                )
                records = list(await session.scalars(select(RawHealthData)))
            assert as_utc(state.last_successful_sync) == START
            assert job.status == JobStatus.failed and job.records_downloaded == int(not first_page)
            assert job.last_error == summary
            assert (job.last_page_token if lane == "sync" else job.page_token) == saved
            if lane == "backfill":
                assert job.completed_at is None and job.retry_count == attempt
            assert [e.kind for e in events] == ["page"] * bool(prefix) + ["failed"] * attempt
            assert not any(e.last_page for e in events)
            if first_page:
                assert checkpoint is None
            else:
                assert checkpoint.event_id == events[0].id
                assert checkpoint.next_token_hash == hashlib.sha256(b"saved").hexdigest()
            assert len(receipts) == attempt and receipts[0].starts_first
            if attempt == 2:
                assert receipts[1].starts_first == first_page
                assert receipts[1].parent_event_id == (None if first_page else events[0].id)
            assert {r.google_data_point_id for r in records} == {
                r["name"] for r in ([old] if first_page else [old, first])
            }
            assert all(r.source_deleted_at is None for r in records)
        assert calls == ([None] * 4 if first_page else [None] + ["saved"] * 4)
        assert all(s.close_calls == 1 and s.closed.is_set() for s in streams)
        rows = await stored(sessions)
        assert [r.body for r in rows] == [wire for _, wire in prefix] + [b"retry", bad] * 2
        assert [r.status_code for r in rows] == [s for s, _ in prefix] + [503, status] * 2
        assert [r.attempt for r in rows] == ([0] if prefix else []) + [0, 1] * 2
        assert all(r.complete and r.body_sha256 == hashlib.sha256(r.body).hexdigest() for r in rows)
        assert all(r.body_size == len(r.body) and r.data_type == kind for r in rows)
        assert all(
            r.error == ("TrailingEncodedBytes" if compression and r.body == bad else None)
            for r in rows
        )
        assert "private-marker" not in caplog.text and "secret-access-token" not in caplog.text
        await runner.run(google)
        assert calls[-1] == saved
        async with sessions() as session:
            state = await session.get(SyncState, kind)
            job = state if lane == "sync" else await session.get(BackfillJob, runner.job_id)
            events = list(await session.scalars(select(RetrievalEvent).order_by(RetrievalEvent.id)))
        assert job.status == JobStatus.completed and job.last_error is None
        assert (job.last_page_token if lane == "sync" else job.page_token) is None
        assert [e.kind for e in events][-2:] == ["page", "completed"]
        if lane == "sync":
            assert as_utc(state.last_successful_sync) == END
        else:
            assert job.completed_at is not None
    finally:
        await google.http.aclose()


async def client_rejects(kind, raw, error_type, caplog=None):
    """Every encoding of ``raw`` is rejected on its first exchange, without a chain."""
    for encoding in ENCODINGS:
        calls = []

        def handler(request, calls=calls, encoding=encoding):
            calls.append(request)
            headers = {"content-encoding": encoding.removeprefix("raw-")}
            return httpx.Response(200, headers=headers, content=compressed(raw, encoding))

        google = client(handler, google_api_max_retries=4)
        async with google.http:
            with pytest.raises(error_type) as caught:
                await google.list_data_points(REGISTRY[kind], START, END, page_token="saved")
        assert error_summary(caught.value) == f"{error_type.__name__} (invalid_response)"
        assert not is_transient(caught.value) and len(calls) == 1
        assert caught.value.__cause__ is caught.value.__context__ is None
        if caplog is not None:
            assert "private-marker" not in str(caught.value) + caplog.text
