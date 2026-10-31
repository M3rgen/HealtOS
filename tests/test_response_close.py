"""Response deadlines terminate streams without losing bytes or durable progress."""

import asyncio
import gzip
import hashlib
import json
import zlib

import httpx
import pytest
from compressed_records import compressed
from raw_response_records import FlowingBody, PausedBody, client, no_secret, stored
from retrieval_records import END, START, Runner, point
from sqlalchemy import select

from app.core.timeutil import as_utc
from app.google_health import response_body, response_close, response_decode
from app.google_health.client import GoogleHealthError
from app.google_health.data_types import REGISTRY
from app.models.retrieval import RetrievalCheckpoint, RetrievalEvent, RetrievalReceipt
from app.models.state import BackfillJob, JobStatus, RawHealthData, SyncState
from app.services.storage import store_records
from app.sync.errors import error_summary, is_transient
from app.sync.ownership import run_ownership


@pytest.mark.parametrize("phase", ["close", "read-error", "body", "buffered-body", "empty-body"])
@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate"])
@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize("status", [200, 307, 308])
async def test_response_budget_archives_attempts_and_bounds_retries(
    sessions, monkeypatch, caplog, phase, encoding, recover, status
):
    monkeypatch.setattr(response_close, "RESPONSE_CLOSE_SECONDS", 0.01)
    assert 0 < response_body.RESPONSE_BODY_SECONDS <= 60
    monkeypatch.setattr(response_body, "RESPONSE_BODY_SECONDS", 0.02)
    payload = b' {"dataPoints": [], "private-body": "synthetic"}\n'
    wire = {"identity": payload, "gzip": gzip.compress(payload), "deflate": zlib.compress(payload)}[
        encoding
    ]
    if phase == "read-error":
        wire = wire[:12]
    streams, calls = [], []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, content=b"retry")
        if recover and len(calls) == 3:
            return httpx.Response(200, content=b'{"dataPoints": []}')
        stream = (
            FlowingBody(
                wire, buffered="empty" if phase == "empty-body" else phase == "buffered-body"
            )
            if "body" in phase
            else PausedBody(wire, phase)
        )
        streams.append(stream)
        return httpx.Response(
            status,
            stream=stream,
            headers={
                "content-encoding": encoding,
                "date": "synthetic",
                "location": "https://example.invalid:bad/private-marker"
                if status == 307
                else "https://[invalid-ipv6]/private-marker",
            },
        )

    google = client(handler, sessions, google_api_max_retries=2)
    task = asyncio.create_task(google.list_data_points(REGISTRY["steps"], START, END))
    try:
        await asyncio.wait((task,), timeout=2)  # observation only; never cancels the request
        assert task.done(), "response budget did not terminate the request"
        if recover:
            page = task.result()
            assert page.data_points == [] and page.next_page_token is None
            await google.keep(page.responses)
        else:
            with pytest.raises(GoogleHealthError) as err:
                task.result()
            assert error_summary(err.value) == "GoogleHealthError (connection)"
            assert is_transient(err.value)
            assert isinstance(
                err.value.__cause__,
                response_body.ResponseBodyTimeout
                if "body" in phase
                else response_close.ResponseCloseTimeout
                if phase == "close"
                else httpx.ReadTimeout,
            )
        assert len(calls) == 3
        assert all(s.close_calls == 1 and s.closed.is_set() for s in streams)
        rows = await stored(sessions)
        assert [r.body for r in rows] == [
            b"retry",
            streams[0].wire,
            b'{"dataPoints": []}' if recover else streams[1].wire,
        ]
        assert [r.attempt for r in rows] == [0, 1, 2]
        assert [r.status_code for r in rows] == [503, status, 200 if recover else status]
        for row in rows:
            assert row.body_sha256 == hashlib.sha256(row.body).hexdigest()
            assert row.body_size == len(row.body)
            no_secret(row)
        for stream, row in zip(streams, rows[1:], strict=False):
            if "body" in phase:
                extra = len(stream.wire) - len(wire)
                assert extra == 0 if phase == "empty-body" else extra > 0
            assert row.complete == (phase == "close")
            assert row.error == (
                "ResponseBodyTimeout"
                if "body" in phase
                else "ResponseCloseTimeout"
                if phase == "close"
                else "ReadTimeout"
            )
            assert row.headers == {"content-encoding": encoding, "date": "synthetic"}
        assert "private-body" not in caplog.text and "secret-access-token" not in caplog.text
        assert not any(
            t is not asyncio.current_task() and t.get_coro().__name__ == "close_response"
            for t in asyncio.all_tasks()
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await google.http.aclose()


@pytest.mark.parametrize("lane", ["sync", "backfill"])
@pytest.mark.parametrize(
    "phase",
    [
        "close",
        "read-error",
        "body",
        "wire",
        "gzip-wire",
        "deflate-wire",
        "gzip-truncated",
        "deflate-truncated",
        "raw-deflate-truncated",
        "gzip-checksum",
        "deflate-checksum",
        "gzip-limit",
        "deflate-limit",
        "raw-deflate-limit",
        "gzip-members-limit",
        "gzip-stack-limit",
    ],
)
async def test_response_failure_releases_ownership_and_resumes_checkpoint(
    sessions, monkeypatch, caplog, lane, phase, stream_errors=None, encoding="identity"
):
    monkeypatch.setattr(response_close, "RESPONSE_CLOSE_SECONDS", 0.01)
    monkeypatch.setattr(response_body, "RESPONSE_BODY_SECONDS", 0.02)
    runner = Runner(sessions, lane, kind="steps")
    old, first, last = [point(kind="steps") for _ in range(3)]
    async with sessions.begin() as session:
        await store_records(session, REGISTRY["steps"], [old])
        session.add(SyncState(data_type="steps", last_successful_sync=START))
    good = json.dumps({"dataPoints": [first], "nextPageToken": "saved"}).encode()
    rejected = json.dumps({"dataPoints": [last], "private-body": "synthetic"}).encode()
    calls, streams = [], []
    encoding, _, damage = phase.rpartition("-") if stream_errors is None else (encoding, "", "")
    wire_limited = phase == "wire" or damage == "wire"
    limited = damage == "limit"
    decoding = damage in {"truncated", "checksum", "limit"} or damage == "wire"
    if limited:
        monkeypatch.setattr(response_decode, "MAX_DECODED_BYTES", len(rejected) - 1)
    if encoding == "gzip-members":
        wire = compressed(rejected[:20], "gzip") + compressed(rejected[20:], "gzip")
        encoding = "gzip"
    elif encoding == "gzip-stack":
        wire = compressed(compressed(rejected, "gzip"), "deflate")
        encoding = "gzip, deflate"
    else:
        wire = compressed(rejected, encoding, damage) if decoding or stream_errors else rejected
    if wire_limited:
        monkeypatch.setattr(response_body, "MAX_WIRE_BYTES", max(len(good), len(wire)) + 1)
    unexpected = stream_errors and not issubclass(
        stream_errors[0] or stream_errors[1], httpx.TransportError
    )
    rejected_attempts = 1 if limited or wire_limited or unexpected else 2

    def handler(request):
        calls.append(request.url.params.get("pageToken"))
        if len(calls) == 1:
            return httpx.Response(200, content=good)
        if len(calls) == 2:
            return httpx.Response(503, content=b"retry")
        if len(calls) <= 2 + rejected_attempts:
            stream = (
                PausedBody(wire, "close", read_error=stream_errors[0], close_error=stream_errors[1])
                if stream_errors
                else (
                    FlowingBody(wire, buffered=wire_limited, finish=decoding and not wire_limited)
                    if phase in {"body", "wire"} or decoding
                    else PausedBody(wire, phase)
                )
            )
            if decoding or wire_limited or stream_errors:
                stream.release.set()
            streams.append(stream)
            return httpx.Response(
                200,
                stream=stream,
                headers={"content-encoding": encoding.removeprefix("raw-")}
                if decoding or stream_errors
                else {},
            )
        return httpx.Response(200, json={"dataPoints": [last]})

    google = client(handler, sessions, google_api_max_retries=2)
    exchange = google._exchange

    async def bounded_exchange(*args, **kwargs):
        # Bound HTTP reads, closes and retries, independently of database commits.
        # A durable resume must not be cancelled while committing its receipt just
        # because a loaded runner takes longer to schedule the database worker.
        return await asyncio.wait_for(exchange(*args, **kwargs), 2)

    monkeypatch.setattr(google, "_exchange", bounded_exchange)
    try:
        with pytest.raises(GoogleHealthError) as caught:
            await runner.run(google)
        if unexpected:
            assert str(caught.value) == "Google Health response stream failed"
            assert not is_transient(caught.value)
            assert caught.value.__cause__ is caught.value.__context__ is None
        if wire_limited:
            assert (
                error_summary(caught.value) == "ResponseBodyTooLargeError (wire_response_too_large)"
            )
            assert not is_transient(caught.value)
        if limited:
            assert str(caught.value) == "Google Health response exceeded its decoded byte budget"
            assert not is_transient(caught.value)
            assert caught.value.__cause__ is caught.value.__context__ is None
        assert calls == [None] + ["saved"] * (1 + rejected_attempts)
        async with sessions() as session:
            state = await session.get(SyncState, "steps")
            job = state if lane == "sync" else await session.get(BackfillJob, runner.job_id)
            checkpoint = await session.scalar(select(RetrievalCheckpoint))
            events = list(await session.scalars(select(RetrievalEvent).order_by(RetrievalEvent.id)))
            records = list(await session.scalars(select(RawHealthData)))
        assert as_utc(state.last_successful_sync) == START
        assert job.status == JobStatus.failed and job.records_downloaded == 1
        assert job.last_error == (
            "ResponseBodyTooLargeError (wire_response_too_large)"
            if wire_limited
            else "ResponseStreamError"
            if unexpected
            else "GoogleHealthError (response_too_large)"
            if limited
            else "GoogleHealthError (connection)"
        )
        if lane == "backfill":
            assert job.completed_at is None
        assert (job.last_page_token if lane == "sync" else job.page_token) == "saved"
        assert [e.kind for e in events] == ["page", "failed"]
        assert not any(e.last_page for e in events) and checkpoint.event_id == events[0].id
        assert checkpoint.next_token_hash == hashlib.sha256(b"saved").hexdigest()
        assert {r.google_data_point_id for r in records} == {old["name"], first["name"]}
        assert all(r.source_deleted_at is None for r in records)
        async with sessions() as session:
            assert len(list(await session.scalars(select(RetrievalReceipt)))) == 1
        rows = await stored(sessions)
        assert [r.body for r in rows] == [good, b"retry", *(s.wire for s in streams)]
        assert [r.attempt for r in rows] == [0, 0] + list(range(1, 1 + rejected_attempts))
        assert [r.complete for r in rows] == [True, True] + [
            stream_errors[0] is None
            if stream_errors
            else phase == "close" or (decoding and not wire_limited)
        ] * rejected_attempts
        if stream_errors:
            error = (stream_errors[0] or stream_errors[1]).__name__
            assert [r.error for r in rows] == [None, None] + [error] * rejected_attempts
            assert [r.status_code for r in rows] == [200, 503] + [200] * rejected_attempts
            assert all(s.close_calls == 1 and s.closed.is_set() for s in streams)
            assert not any(t.get_coro().__name__ == "close_response" for t in asyncio.all_tasks())
        if decoding or wire_limited:
            error = (
                "ResponseBodyLimitExceeded"
                if wire_limited
                else "ResponseDecodeLimitExceeded"
                if limited
                else "DecodingError"
            )
            assert [r.error for r in rows] == [None, None] + [error] * rejected_attempts
        assert all(r.body_size == len(r.body) for r in rows)
        assert all(r.body_sha256 == hashlib.sha256(r.body).hexdigest() for r in rows)
        assert "private-body" not in caplog.text and "secret-access-token" not in caplog.text
        assert "saved" not in caplog.text
        async with run_ownership(sessions, "steps", wait=False) as owned:
            assert owned is not None
        await runner.run(google)
        assert calls[-1] == "saved"
        async with sessions() as session:
            state = await session.get(SyncState, "steps")
            job = state if lane == "sync" else await session.get(BackfillJob, runner.job_id)
            events = list(await session.scalars(select(RetrievalEvent).order_by(RetrievalEvent.id)))
            receipts = list(
                await session.scalars(
                    select(RetrievalReceipt).order_by(RetrievalReceipt.started_at)
                )
            )
        assert job.status == JobStatus.completed
        assert (job.last_page_token if lane == "sync" else job.page_token) is None
        assert [e.kind for e in events] == ["page", "failed", "page", "completed"]
        assert receipts[1].parent_event_id == events[0].id and not receipts[1].starts_first
        if lane == "sync":
            assert as_utc(state.last_successful_sync) == END
        else:
            assert job.completed_at is not None
    finally:
        await google.http.aclose()


@pytest.mark.parametrize("lane", ["sync", "backfill"])
@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate"])
@pytest.mark.parametrize(
    ("read_error", "close_error"),
    [
        (r, c)
        for r in [None, RuntimeError, ValueError, OSError, httpx.ReadTimeout]
        for c in [None, RuntimeError, ValueError, OSError, httpx.CloseError]
        if r is not None or c not in {None, httpx.CloseError}
    ],
)
async def test_unexpected_stream_failure_preserves_archive_and_progress(
    sessions, monkeypatch, caplog, lane, encoding, read_error, close_error
):
    await test_response_failure_releases_ownership_and_resumes_checkpoint(
        sessions, monkeypatch, caplog, lane, "unexpected", (read_error, close_error), encoding
    )


async def test_stream_finishing_within_body_budget_is_complete(sessions, monkeypatch):
    monkeypatch.setattr(response_body, "RESPONSE_BODY_SECONDS", 0.1)
    wire = b' {"dataPoints": []}\n'
    stream = FlowingBody(wire, finish=True)
    stream.release.set()
    google = client(lambda _: httpx.Response(200, stream=stream), sessions)
    try:
        page = await google.list_data_points(REGISTRY["steps"], START, END)
        await google.keep(page.responses)
        (row,) = await stored(sessions)
        assert page.data_points == [] and row.body == wire
        assert row.complete and row.error is None and stream.close_calls == 1
    finally:
        await google.http.aclose()


@pytest.mark.parametrize("lane", ["sync", "backfill"])
async def test_body_retry_success_stores_only_the_valid_page(sessions, monkeypatch, lane):
    monkeypatch.setattr(response_body, "RESPONSE_BODY_SECONDS", 0.02)
    stream = FlowingBody(json.dumps({"dataPoints": [point(kind="steps")]}).encode())
    stream.release.set()
    record, calls = point(kind="steps"), []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, stream=stream)
        return httpx.Response(200, json={"dataPoints": [record]})

    google = client(handler, sessions, google_api_max_retries=1)
    try:
        await Runner(sessions, lane, kind="steps").run(google)
        rows = await stored(sessions)
        assert len(calls) == len(rows) == 2 and rows[0].body == stream.wire
        assert not rows[0].complete and rows[0].error == "ResponseBodyTimeout"
        assert rows[1].complete and rows[1].error is None
        async with sessions() as session:
            (raw,) = list(await session.scalars(select(RawHealthData)))
            assert raw.google_data_point_id == record["name"] and raw.response_id == rows[1].id
            events = list(await session.scalars(select(RetrievalEvent)))
            assert [e.kind for e in events] == ["page", "completed"] and events[0].last_page
    finally:
        await google.http.aclose()


@pytest.mark.parametrize("lane", ["sync", "backfill"])
@pytest.mark.parametrize("phase", ["read", "close", "wire"])
@pytest.mark.parametrize("status", [200, 307, 308])
async def test_external_cancel_does_not_retry_body_timeout(
    sessions,
    monkeypatch,
    lane,
    phase,
    status,
    stream_errors=None,
    encoding="identity",
    cleanup="timeout",
):
    monkeypatch.setattr(response_body, "RESPONSE_BODY_SECONDS", 0.02 if phase == "close" else 60)
    monkeypatch.setattr("app.google_health.client.KEEP_ON_CANCEL_SECONDS", 0.02)
    stream = FlowingBody(b'{"dataPoints": []}')
    if stream_errors:
        stream = PausedBody(
            compressed(b'{"dataPoints": []}', encoding),
            "close",
            read_error=stream_errors[0],
            close_error=stream_errors[1],
        )
    if phase == "wire":
        monkeypatch.setattr(response_body, "MAX_WIRE_BYTES", 5)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            stream=stream,
            headers={
                "content-encoding": encoding,
                "location": "https://example.invalid:bad/private-marker"
                if status == 307
                else "https://[invalid-ipv6]/private-marker",
            },
        )

    google = client(handler, sessions)
    task = asyncio.create_task(Runner(sessions, lane, kind="steps").run(google))
    try:
        await asyncio.wait_for((stream.reading if phase == "read" else stream.closing).wait(), 2)
        task.cancel("secret-access-token")
        if cleanup != "timeout":
            if cleanup == "cancel-again":
                await asyncio.sleep(0)
                task.cancel("secret-access-token")
            stream.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        (row,) = await stored(sessions)
        assert len(calls) == 1 and row.body == stream.wire
        assert row.complete == (bool(stream_errors) and stream_errors[0] is None)
        assert row.body_sha256 == hashlib.sha256(stream.wire).hexdigest()
        assert row.status_code == status
        assert row.error == "CancelledError" and stream.close_calls == 1 and stream.closed.is_set()
        async with run_ownership(sessions, "steps", wait=False) as owned:
            assert owned is not None
        async with sessions() as session:
            events = list(await session.scalars(select(RetrievalEvent)))
            assert [e.kind for e in events] == ["cancelled"]
            assert await session.scalar(select(RetrievalCheckpoint)) is None
            assert await session.scalar(select(RawHealthData)) is None
        assert not any(t.get_coro().__name__ == "close_response" for t in asyncio.all_tasks())
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await google.http.aclose()


@pytest.mark.parametrize("lane", ["sync", "backfill"])
# Every lane, error and cleanup combination runs; the wire encodings rotate.
@pytest.mark.parametrize(
    ("read_error", "encoding"),
    [(None, "identity"), (RuntimeError, "gzip"), (ValueError, "deflate"), (OSError, "identity")],
)
@pytest.mark.parametrize("cleanup", ["release", "cancel-again", "timeout"])
async def test_cancel_during_unexpected_stream_cleanup(
    sessions, monkeypatch, caplog, lane, read_error, encoding, cleanup
):
    await test_external_cancel_does_not_retry_body_timeout(
        sessions, monkeypatch, lane, "unexpected", 200, (read_error, OSError), encoding, cleanup
    )
    assert "private-body" not in caplog.text and "secret-access-token" not in caplog.text
