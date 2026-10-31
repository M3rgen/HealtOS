"""Verbatim Google responses (raw_api_responses) and source-deletion events, on SQLite."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import zlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from compressed_records import compressed
from raw_response_records import (
    NAME,
    SPEC,
    PausedBody,
    Tokens,
    body,
    client,
    no_secret,
    steps,
    stored,
)
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from typer.testing import CliRunner

from app.cli import main as cli
from app.core.config import Settings
from app.core.provenance import provenance
from app.db.base import Base
from app.google_health.client import GoogleHealthClient, GoogleHealthError
from app.google_health.data_types import REGISTRY
from app.models.raw_versions import RawApiResponse, RawHealthDataEvent, RawHealthDataVersion
from app.models.state import RawHealthData
from app.services.raw_history import record_history
from app.services.responses import DatabaseResponseArchive, verify_responses
from app.services.storage import store_page
from app.sync.deletions import mark_source_deletions
from app.sync.incremental import IncrementalSync


async def test_a_page_keeps_every_exchange_byte_for_byte(sessions) -> None:
    calls = []
    page_body = body(steps(1), token="next")

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, content=b'{"error": {"status": "UNAVAILABLE"}}')
        return httpx.Response(200, content=page_body, headers={"content-type": "application/json"})

    google = client(handler, sessions)
    start = datetime(2026, 8, 17, tzinfo=UTC)
    page = await google.list_data_points(SPEC, start, start + timedelta(hours=1))
    assert calls[-1].headers["accept-encoding"] == "identity"
    assert [(item.status_code, item.attempt) for item in page.responses] == [(503, 0), (200, 1)]
    assert page.responses[-1].body == page_body
    async with sessions.begin() as session:
        stats = await store_page(session, SPEC, page)
    assert stats.inserted == 1
    rows = await stored(sessions)
    assert [row.body for row in rows] == [b'{"error": {"status": "UNAVAILABLE"}}', page_body]
    assert rows[1].query == calls[-1].url.query.decode()
    assert "pageSize=" in rows[1].query and rows[1].path.endswith("/dataTypes/steps/dataPoints")
    assert rows[1].headers == {
        "content-type": "application/json",
        "content-length": str(len(page_body)),
    }
    for row in rows:
        no_secret(row)
    async with sessions() as session:
        raw = await session.scalar(select(RawHealthData))
    assert raw is not None and raw.response_id == rows[1].id


async def test_a_rollup_keeps_its_request_body(sessions) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"rollupDataPoints": []}')

    start = datetime(2026, 8, 17, tzinfo=UTC)
    page = await client(handler).list_data_points(
        REGISTRY["total-calories"], start, start + timedelta(hours=1)
    )
    (exchange,) = page.responses
    assert exchange.method == "POST" and exchange.query is None
    assert json.loads(exchange.request_body or b"")["windowSize"] == "60s"


async def test_failures_and_cut_off_bodies_are_kept(sessions) -> None:
    class CutOff(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"dataPoints": [{"name": '
            raise httpx.ReadTimeout("slow")

    def handler(request: httpx.Request) -> httpx.Response:
        if handler.calls == 0:
            handler.calls += 1
            return httpx.Response(200, stream=CutOff())
        handler.calls += 1
        return httpx.Response(403, content=b'{"error": {"status": "PERMISSION_DENIED"}}')

    handler.calls = 0
    google = client(handler, sessions, google_api_max_retries=1)
    start = datetime(2026, 8, 17, tzinfo=UTC)
    with pytest.raises(GoogleHealthError, match="403"):
        await google.list_data_points(SPEC, start, start + timedelta(hours=1))
    rows = await stored(sessions)
    assert [(row.status_code, row.complete, row.error, row.body) for row in rows] == [
        (200, False, "ReadTimeout", b'{"dataPoints": [{"name": '),
        (403, True, None, b'{"error": {"status": "PERMISSION_DENIED"}}'),
    ]
    assert {row.source for row in rows} == {"sync"} and rows[0].data_type == "steps"


async def test_identity_is_kept(sessions) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"healthUserId": "u-1"}')

    assert (await client(handler, sessions).get_identity())["healthUserId"] == "u-1"
    (row,) = await stored(sessions)
    assert (row.source, row.body) == ("identity", b'{"healthUserId": "u-1"}')
    assert row.path.endswith("/users/me/identity") and row.data_type is None


async def test_records_and_their_page_commit_together(sessions) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body(steps(1)))

    start = datetime(2026, 8, 17, tzinfo=UTC)
    page = await client(handler).list_data_points(SPEC, start, start + timedelta(hours=1))
    with pytest.raises(RuntimeError, match="crash"):
        async with sessions.begin() as session:
            await store_page(session, SPEC, page)
            raise RuntimeError("crash")
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RawApiResponse)) == 0
        assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 0


async def test_sync_tags_responses_and_keeps_them_when_storing_fails(sessions, monkeypatch) -> None:
    pages = [body(steps(1)), body(steps(2))]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=pages.pop(0))

    google = client(handler, sessions)
    incremental = IncrementalSync(
        Settings(_env_file=None, app_env="test"), sessions, google, {"steps": SPEC}
    )
    await incremental.run("steps")
    (first,) = await stored(sessions)
    assert first.source == "sync" and first.context_id.startswith("sync:")

    import app.services.storage as storage

    async def broken(*args, **kwargs):
        raise RuntimeError("normalizer bug")

    monkeypatch.setattr(storage, "_upsert_normalized_many", broken)
    with pytest.raises(RuntimeError, match="normalizer bug"):
        await incremental.run("steps")
    rows = await stored(sessions)
    assert len(rows) == 2 and rows[1].body == body(steps(2))  # kept although records rolled back
    async with sessions() as session:
        raw = await session.scalar(select(RawHealthData))
    assert raw is not None and raw.raw_json == steps(1) and raw.response_id == first.id


async def test_versions_link_to_the_page_they_came_from(sessions) -> None:
    pages = [body(steps(1)), body(steps(2))]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=pages.pop(0))

    google = client(handler)
    start = datetime(2026, 8, 17, tzinfo=UTC)
    for _ in range(2):
        page = await google.list_data_points(SPEC, start, start + timedelta(hours=1))
        async with sessions.begin() as session:
            await store_page(session, SPEC, page)
    rows = await stored(sessions)
    async with sessions() as session:
        version = await session.scalar(select(RawHealthDataVersion))
        raw = await session.scalar(select(RawHealthData))
    assert version is not None and version.response_id == rows[0].id
    assert raw is not None and raw.response_id == rows[1].id


async def test_responses_and_events_are_append_only(sessions) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body(steps(1)))

    start = datetime(2026, 8, 17, tzinfo=UTC)
    page = await client(handler).list_data_points(SPEC, start, start + timedelta(hours=1))
    async with sessions.begin() as session:
        await store_page(session, SPEC, page)
        await session.execute(text("UPDATE raw_health_data SET source_deleted_at = '2026-08-18'"))
    for statement in (
        "UPDATE raw_api_responses SET body = x'00'",
        "DELETE FROM raw_api_responses",
        "UPDATE raw_health_data_events SET event = 'x'",
        "DELETE FROM raw_health_data_events",
    ):
        with pytest.raises((IntegrityError, OperationalError), match="refused"):
            async with sessions.begin() as session:
                await session.execute(text(statement))


async def test_deletion_marks_and_returns_are_recorded(sessions) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body(steps(1)))

    google = client(handler)
    start = datetime(2026, 8, 17, 9, tzinfo=UTC)
    end = start + timedelta(hours=3)
    async with sessions.begin() as session:
        await store_page(session, SPEC, await google.list_data_points(SPEC, start, end))
    async with sessions.begin() as session:
        marked = await mark_source_deletions(
            session, SPEC, start, end, datetime.now(UTC) + timedelta(seconds=1)
        )
    assert marked == 1
    async with sessions() as session:
        deleted_at = await session.scalar(select(RawHealthData.source_deleted_at))
    async with sessions.begin() as session:
        await store_page(session, SPEC, await google.list_data_points(SPEC, start, end))
    async with sessions() as session:
        events = (
            await session.scalars(select(RawHealthDataEvent).order_by(RawHealthDataEvent.id))
        ).all()
        history = await record_history(session, SPEC.name, google_id=NAME)
    assert [event.event for event in events] == ["source-deleted", "source-returned"]
    assert events[1].previous_deleted_at == deleted_at  # the mark survives its clearing
    assert [item["event"] for item in history[0]["events"]] == ["source-deleted", "source-returned"]


def test_cli_exports_bytes_and_verify_catches_tampering(tmp_path, monkeypatch) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'responses.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    page_body = body(steps(1))

    async def prepare() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=page_body)

        start = datetime(2026, 8, 17, tzinfo=UTC)
        page = await client(handler).list_data_points(SPEC, start, start + timedelta(hours=1))
        async with sessions.begin() as session:
            await store_page(session, SPEC, page)

    asyncio.run(prepare())
    monkeypatch.setattr(cli, "SessionLocal", sessions)
    runner = CliRunner()
    target = tmp_path / "page.json"
    exported = runner.invoke(cli.app, ["export-response", "1", "--output", str(target)])
    assert exported.exit_code == 0, exported.output
    assert target.read_bytes() == page_body
    listed = runner.invoke(cli.app, ["responses", "--data-type", "steps"])
    assert json.loads(listed.output)[0]["body_sha256"]
    assert runner.invoke(cli.app, ["verify"]).exit_code == 0

    async def tamper() -> list[str]:
        async with sessions.begin() as session:
            await session.execute(text("DROP TRIGGER raw_api_responses_no_update"))
            await session.execute(text("UPDATE raw_api_responses SET body = x'7b7d'"))
        async with sessions() as session:
            return (await verify_responses(session))[1]

    assert asyncio.run(tamper()) == ["stored Google response 1 no longer matches its SHA-256"]
    verify = runner.invoke(cli.app, ["verify"])
    assert verify.exit_code == 1
    assert (
        "protective trigger missing: raw_api_responses.raw_api_responses_no_update" in verify.output
    )
    assert runner.invoke(cli.app, ["export-response", "1"]).exit_code == 1
    asyncio.run(engine.dispose())


async def test_verify_reports_a_redefined_sqlite_guard(sessions) -> None:
    from app.services.raw_history import verify_guards

    async with sessions() as session:
        assert await verify_guards(session) == []
    async with sessions.begin() as session:
        await session.execute(text("DROP TRIGGER raw_api_responses_no_delete"))
        await session.execute(
            text(
                "CREATE TRIGGER raw_api_responses_no_delete BEFORE DELETE ON raw_api_responses "
                "WHEN 0 BEGIN SELECT RAISE(ABORT, 'refused'); END"
            )
        )
    async with sessions() as session:
        assert await verify_guards(session) == [
            "protective trigger raw_api_responses.raw_api_responses_no_delete was redefined"
        ]


@pytest.mark.parametrize(
    ("encoding", "damage"),
    [(e, "truncated") for e in ["gzip", "deflate", "raw-deflate"]]
    + [(e, "checksum") for e in ["gzip", "deflate"]],
)
@pytest.mark.parametrize("recover", [False, True])
async def test_invalid_compression_is_archived_and_retried(
    sessions, encoding, damage, recover, caplog
):
    payload = body(steps(7))
    wire = compressed(payload, encoding, damage)
    if damage == "truncated":
        inflater = zlib.decompressobj({"gzip": 31, "deflate": 15, "raw-deflate": -15}[encoding])
        assert inflater.decompress(wire) == payload and not inflater.eof
    calls = []

    def handler(request):
        calls.append(request)
        data = payload if recover and len(calls) == 3 else wire
        headers = {} if data is payload else {"content-encoding": encoding.removeprefix("raw-")}
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(data))

    google = client(handler, sessions, google_api_max_retries=2)
    try:
        start = datetime(2026, 8, 17, tzinfo=UTC)
        if recover:
            page = await google.list_data_points(SPEC, start, start + timedelta(hours=1))
            assert page.data_points == [steps(7)]
            async with sessions.begin() as session:
                await store_page(session, SPEC, page)
        else:
            with pytest.raises(GoogleHealthError) as caught:
                await google.list_data_points(SPEC, start, start + timedelta(hours=1))
            cause = caught.value.__cause__
            assert isinstance(cause, httpx.DecodingError)
            assert str(cause) == "Google Health response content decoding failed"
            assert cause.__cause__ is None and cause.__context__ is None
        rows = await stored(sessions)
        assert len(calls) == len(rows) == 3
        assert [r.attempt for r in rows] == list(range(len(rows)))
        for i, row in enumerate(rows):
            expected = payload if recover and i == 2 else wire
            assert row.body == expected and row.body_size == len(expected) and row.complete
            assert row.body_sha256 == hashlib.sha256(expected).hexdigest()
            assert row.error == (None if recover and i == 2 else "DecodingError")
            no_secret(row)
        assert "secret-access-token" not in caplog.text and "private-marker" not in caplog.text
    finally:
        await google.http.aclose()


async def _cancel_when(task: asyncio.Task, started: asyncio.Event) -> None:
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):  # the cancellation is never swallowed
        await task


@pytest.mark.parametrize("phase", ["before", "read", "read-error", "close"])
@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate"])
@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("cleanup", ["release", "cancel-again", "timeout"])
async def test_cancellation_keeps_wire_bytes_and_closes_the_response(
    sessions, monkeypatch, phase, encoding, retry, cleanup
) -> None:
    import app.google_health.client as module

    decoded = body(steps(7))
    wire = {"identity": decoded, "gzip": gzip.compress(decoded), "deflate": zlib.compress(decoded)}[
        encoding
    ]
    if phase in {"read", "read-error"}:
        wire = wire[:12]  # Incomplete compressed data must never be decoded.
    stream = PausedBody(wire, phase)
    calls = []
    headers = {
        "content-type": "application/json",
        "content-encoding": encoding,
        "content-length": str(len(wire) + (10 if phase in {"read", "read-error"} else 0)),
        "date": "Mon, 17 Aug 2026 10:00:00 GMT",
        "retry-after": "1",
        "set-cookie": "secret-access-token",
        "authorization": "secret-access-token",
    }

    def handler(request):
        calls.append(request)
        if retry and len(calls) == 1:
            return httpx.Response(503, content=b"retry")
        return httpx.Response(200, headers=headers, stream=stream)

    google = client(handler, sessions)
    monkeypatch.setattr(module, "KEEP_ON_CANCEL_SECONDS", 0.1)
    start = datetime(2026, 8, 17, tzinfo=UTC)
    with provenance(source="backfill", context_id="backfill:synthetic"):
        task = asyncio.create_task(google.list_data_points(SPEC, start, start + timedelta(hours=1)))
    try:
        await asyncio.wait_for(
            (stream.closing if phase in {"close", "read-error"} else stream.reading).wait(), 5
        )
        task.cancel("secret-access-token")
        await asyncio.wait_for(stream.closing.wait(), 5)
        if cleanup == "cancel-again":
            task.cancel("secret-access-token")
            await asyncio.sleep(0)
        if cleanup != "timeout":
            stream.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        await asyncio.wait_for(stream.closed.wait(), 1)
        rows = await stored(sessions)
        assert len(calls) == 1 + retry and len(rows) == 1 + retry
        if retry:
            assert (rows[0].body, rows[0].attempt, rows[0].complete) == (b"retry", 0, True)
        row = rows[-1]
        expected = b"" if phase == "before" else wire
        assert row.body == expected and row.body_size == len(expected)
        assert row.body_sha256 == hashlib.sha256(expected).hexdigest()
        assert row.complete == (phase == "close") and row.error == "CancelledError"
        assert row.attempt == int(retry) and row.status_code == 200
        assert (row.source, row.context_id, row.data_type) == (
            "backfill",
            "backfill:synthetic",
            "steps",
        )
        assert row.method == "GET" and row.path.endswith("/dataTypes/steps/dataPoints")
        assert row.query == calls[-1].url.query.decode() and row.request_body is None
        assert row.headers == {
            k: v for k, v in headers.items() if k not in {"authorization", "set-cookie"}
        }
        assert stream.close_calls == 1
        for item in rows:
            no_secret(item)
            assert "secret-access-token" not in str(item.error)
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 0
    finally:
        stream.release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await google.http.aclose()


async def test_a_page_received_before_a_cancellation_is_kept(sessions, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body(steps(1)))

    import app.services.storage as storage

    started = asyncio.Event()

    async def restart_mid_store(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()  # cancelled here, like a deploy stopping the app

    monkeypatch.setattr(storage, "_upsert_normalized_many", restart_mid_store)
    incremental = IncrementalSync(
        Settings(_env_file=None, app_env="test"),
        sessions,
        client(handler, sessions),
        {"steps": SPEC},
    )
    await _cancel_when(asyncio.create_task(incremental.run("steps")), started)
    (row,) = await stored(sessions)
    assert row.body == body(steps(1)) and row.complete
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 0


@pytest.mark.parametrize("header", ["60", "inf", "NaN", "bad"])
async def test_a_response_received_before_a_cancelled_retry_is_kept(sessions, header) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503, headers={"Retry-After": header}, content=b'{"error": {"status": "UNAVAILABLE"}}'
        )

    started = asyncio.Event()

    async def cancelled_backoff(_delay):
        assert 0 <= _delay <= 60
        started.set()
        await asyncio.Event().wait()

    google = GoogleHealthClient(
        Settings(_env_file=None, google_api_min_request_interval_seconds=0),
        Tokens(),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=cancelled_backoff,
        archive=DatabaseResponseArchive(sessions),
    )
    start = datetime(2026, 8, 17, tzinfo=UTC)
    task = asyncio.create_task(google.list_data_points(SPEC, start, start + timedelta(hours=1)))
    await _cancel_when(task, started)
    (row,) = await stored(sessions)
    assert row.status_code == 503 and row.body == b'{"error": {"status": "UNAVAILABLE"}}'
    assert row.headers["retry-after"] == header and row.complete


@pytest.mark.parametrize("stalled", [False, True])
async def test_a_store_interrupted_by_cancellation_still_finishes(
    sessions, monkeypatch, stalled
) -> None:
    from app.google_health.client import KEEP_ON_CANCEL_SECONDS

    assert 0 < KEEP_ON_CANCEL_SECONDS <= 30
    started = asyncio.Event()
    release, finished = asyncio.Event(), asyncio.Event()

    class SlowArchive(DatabaseResponseArchive):
        async def keep(self, responses):
            started.set()
            if stalled:
                await release.wait()
            else:
                await asyncio.sleep(0.05)
            await super().keep(responses)
            finished.set()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"healthUserId": "synthetic"}')

    google = client(handler)
    google.archive = SlowArchive(sessions)
    if stalled:
        monkeypatch.setattr("app.google_health.client.KEEP_ON_CANCEL_SECONDS", 0.05)
    try:
        await asyncio.wait_for(_cancel_when(asyncio.create_task(google.get_identity()), started), 1)
        assert finished.is_set() != stalled
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await google.http.aclose()
    (row,) = await stored(sessions)
    assert row.source == "identity" and row.body == b'{"healthUserId": "synthetic"}'


async def test_a_backfill_page_received_before_a_cancellation_is_kept(
    sessions, monkeypatch
) -> None:
    import app.services.storage as storage
    from app.google_health.pagination import PageBudget
    from app.models.state import BackfillJob, JobStatus
    from app.sync.backfill import BackfillService

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body(steps(3)))

    started = asyncio.Event()

    async def restart_mid_store(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(storage, "_upsert_normalized_many", restart_mid_store)
    start = datetime(2026, 8, 17, tzinfo=UTC)
    async with sessions.begin() as session:
        job = BackfillJob(
            data_type="steps",
            window_start=start,
            window_end=start + timedelta(days=1),
            status=JobStatus.pending,
        )
        session.add(job)
    backfill = BackfillService(
        Settings(_env_file=None, app_env="test"),
        sessions,
        client(handler, sessions),
        {"steps": SPEC},
    )
    window = backfill._run_next_window("steps", PageBudget())
    await _cancel_when(asyncio.create_task(window), started)
    (row,) = await stored(sessions)
    assert row.body == body(steps(3)) and row.source == "backfill"


async def test_gap_repair_keeps_a_page_whose_records_fail_to_store(sessions, monkeypatch) -> None:
    import app.services.storage as storage
    from scripts.audit_date_gaps import repair_window

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body(steps(5)))

    async def broken(*args, **kwargs):
        raise RuntimeError("normalizer bug")

    monkeypatch.setattr(storage, "_upsert_normalized_many", broken)
    start = datetime(2026, 8, 17, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="normalizer bug"):
        await repair_window(
            client(handler, sessions), sessions, SPEC, start, start + timedelta(days=1), "gap:1"
        )
    (row,) = await stored(sessions)
    assert row.body == body(steps(5)) and row.source == "gap-repair"
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 0
