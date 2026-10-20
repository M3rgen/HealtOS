"""Verbatim Google responses (raw_api_responses) and source-deletion events, on SQLite."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from typer.testing import CliRunner

from app.cli import main as cli
from app.core.config import Settings
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

SPEC = REGISTRY["steps"]
NAME = "users/me/dataTypes/steps/dataPoints/a"


def steps(count: int, name: str = NAME) -> dict:
    return {
        "name": name,
        "steps": {
            "interval": {
                "startTime": "2026-08-17T10:00:00Z",
                "endTime": "2026-08-17T10:01:00Z",
            },
            "count": str(count),
        },
    }


def body(*records: dict, token: str | None = None) -> bytes:
    # Odd spacing and key order on purpose: only the stored bytes keep them.
    payload = json.dumps(
        {"dataPoints": list(records), **({"nextPageToken": token} if token else {})}
    )
    return ("  " + payload.replace(": ", ":   ") + "\n").encode()


class Tokens:
    async def get(self):
        return "secret-access-token"

    def invalidate(self):
        pass


def client(handler, sessions=None, **settings) -> GoogleHealthClient:
    async def no_sleep(_delay):
        return None

    return GoogleHealthClient(
        Settings(_env_file=None, google_api_min_request_interval_seconds=0, **settings),
        Tokens(),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=no_sleep,
        archive=DatabaseResponseArchive(sessions) if sessions else None,
    )


async def stored(sessions) -> list[RawApiResponse]:
    async with sessions() as session:
        return list(
            (await session.scalars(select(RawApiResponse).order_by(RawApiResponse.id))).all()
        )


def no_secret(response: RawApiResponse) -> None:
    for value in (response.query, response.request_body, response.headers, response.path):
        assert "secret-access-token" not in str(value)


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


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_a_compressed_response_is_kept_as_it_came_off_the_wire(
    tmp_path, monkeypatch, encoding
) -> None:
    import gzip
    import hashlib
    import zlib

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'wire.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    decoded = body(steps(7))
    wire = gzip.compress(decoded, mtime=0) if encoding == "gzip" else zlib.compress(decoded)

    def handler(request: httpx.Request) -> httpx.Response:
        # Sent compressed although the client asked for identity; a stream, as on a socket.
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": encoding},
            stream=httpx.ByteStream(wire),
        )

    async def fetch() -> RawApiResponse:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        start = datetime(2026, 8, 17, tzinfo=UTC)
        page = await client(handler).list_data_points(SPEC, start, start + timedelta(hours=1))
        assert page.data_points == [steps(7)]  # parsed from the decoded body
        async with sessions.begin() as session:
            await store_page(session, SPEC, page)
        (row,) = await stored(sessions)
        async with sessions() as session:
            assert await verify_responses(session) == (1, [])
        return row

    row = asyncio.run(fetch())
    assert row.body == wire and row.body != decoded
    assert row.body_sha256 == hashlib.sha256(wire).hexdigest()
    assert row.body_size == len(wire)
    assert row.headers["content-encoding"] == encoding and row.complete and row.error is None
    monkeypatch.setattr(cli, "SessionLocal", sessions)
    target = tmp_path / "page.bin"
    exported = CliRunner().invoke(cli.app, ["export-response", "1", "--output", str(target)])
    assert exported.exit_code == 0, exported.output
    assert target.read_bytes() == wire
    assert f'"content_encoding": "{encoding}"' in exported.output
    asyncio.run(engine.dispose())


async def _cancel_when(task: asyncio.Task, started: asyncio.Event) -> None:
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):  # the cancellation is never swallowed
        await task


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


async def test_a_response_received_before_a_cancelled_retry_is_kept(sessions) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b'{"error": {"status": "UNAVAILABLE"}}')

    started = asyncio.Event()

    async def cancelled_backoff(_delay):
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


async def test_a_store_interrupted_by_cancellation_still_finishes(sessions) -> None:
    from app.google_health.client import KEEP_ON_CANCEL_SECONDS

    assert 0 < KEEP_ON_CANCEL_SECONDS <= 30
    started = asyncio.Event()

    class SlowArchive(DatabaseResponseArchive):
        async def keep(self, responses):
            started.set()
            await asyncio.sleep(0.05)
            await super().keep(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"healthUserId": "synthetic"}')

    google = client(handler)
    google.archive = SlowArchive(sessions)
    await _cancel_when(asyncio.create_task(google.get_identity()), started)
    (row,) = await stored(sessions)
    assert row.source == "identity" and row.body == b'{"healthUserId": "synthetic"}'


async def test_a_backfill_page_received_before_a_cancellation_is_kept(
    sessions, monkeypatch
) -> None:
    import app.services.storage as storage
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
    await _cancel_when(asyncio.create_task(backfill._run_job(job.id)), started)
    (row,) = await stored(sessions)
    assert row.body == body(steps(3)) and row.source == "backfill"
