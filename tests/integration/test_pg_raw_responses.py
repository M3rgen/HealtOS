"""Verbatim Google responses and source-deletion events against PostgreSQL."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.google_health.client import GoogleHealthClient
from app.google_health.data_types import REGISTRY
from app.models.raw_versions import RawApiResponse, RawHealthDataEvent
from app.models.state import RawHealthData
from app.services.raw_history import verify_guards
from app.services.responses import DatabaseResponseArchive, verify_responses
from app.services.storage import store_page
from app.sync.deletions import mark_source_deletions
from app.sync.incremental import IncrementalSync

pytestmark = pytest.mark.integration

SPEC = REGISTRY["steps"]
NAME = "users/me/dataTypes/steps/dataPoints/a"


def page_body(count: int) -> bytes:
    record = {
        "name": NAME,
        "steps": {
            "interval": {"startTime": "2026-08-17T10:00:00Z", "endTime": "2026-08-17T10:01:00Z"},
            "count": str(count),
        },
    }
    # Spacing, key order, a non-ASCII character and an escaped one: all kept as sent.
    return (
        b'{ "nextPageToken" : "",\n  "dataPoints": ['
        + json.dumps(record).encode()
        + ' ], "note": "ş\\u015f" }'.encode()
    )


def google(handler, sessions) -> GoogleHealthClient:
    async def no_sleep(_delay):
        return None

    class Tokens:
        async def get(self):
            return "secret-access-token"

        def invalidate(self):
            pass

    return GoogleHealthClient(
        Settings(_env_file=None, google_api_min_request_interval_seconds=0),
        Tokens(),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=no_sleep,
        archive=DatabaseResponseArchive(sessions),
    )


async def test_sync_stores_pages_byte_for_byte_with_their_records(pg_sessions) -> None:
    bodies = [page_body(1), page_body(2)]
    served = list(bodies)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=served.pop(0))

    incremental = IncrementalSync(
        Settings(_env_file=None, app_env="test"),
        pg_sessions,
        google(handler, pg_sessions),
        {"steps": SPEC},
    )
    await incremental.run("steps")
    await incremental.run("steps")
    async with pg_sessions() as session:
        rows = (await session.scalars(select(RawApiResponse).order_by(RawApiResponse.id))).all()
        raw = await session.scalar(select(RawHealthData))
        history = await session.execute(text("SELECT response_id FROM raw_health_data_versions"))
        assert await verify_responses(session) == (2, [])
        assert await verify_guards(session) == []
    assert [row.body for row in rows] == bodies
    assert all(row.context_id and row.context_id.startswith("sync:") for row in rows)
    assert rows[0].context_id != rows[1].context_id
    assert "secret-access-token" not in repr([(row.query, row.headers) for row in rows])
    assert raw is not None and raw.response_id == rows[1].id
    assert history.scalars().all() == [rows[0].id]


async def test_responses_and_events_refuse_every_change(pg_sessions) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=page_body(1))

    start = datetime(2026, 8, 17, 9, tzinfo=UTC)
    end = start + timedelta(hours=3)
    page = await google(handler, pg_sessions).list_data_points(SPEC, start, end)
    async with pg_sessions.begin() as session:
        await store_page(session, SPEC, page)
        await mark_source_deletions(session, SPEC, start, end, datetime.now(UTC) + timedelta(1))
    for statement in (
        "UPDATE raw_api_responses SET body = '\\x00'",
        "DELETE FROM raw_api_responses",
        "TRUNCATE raw_api_responses",  # refused by the foreign key or the trigger
        "TRUNCATE raw_api_responses CASCADE",  # would reach raw_health_data
        "UPDATE raw_health_data_events SET event = 'x'",
        "DELETE FROM raw_health_data_events",
        "TRUNCATE raw_health_data_events",
    ):
        with pytest.raises(DBAPIError, match="refused|referenced in a foreign key"):
            async with pg_sessions.begin() as session:
                await session.execute(text(statement))
    async with pg_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RawApiResponse)) == 1
        assert await session.scalar(select(func.count()).select_from(RawHealthDataEvent)) == 1


async def test_verify_catches_a_changed_body(pg_sessions) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=page_body(1))

    start = datetime(2026, 8, 17, tzinfo=UTC)
    page = await google(handler, pg_sessions).list_data_points(SPEC, start, start + timedelta(1))
    async with pg_sessions.begin() as session:
        await store_page(session, SPEC, page)
    async with pg_sessions.begin() as session:
        await session.execute(
            text("ALTER TABLE raw_api_responses DISABLE TRIGGER raw_api_responses_append_only")
        )
        await session.execute(text("UPDATE raw_api_responses SET body = body || '\\x20'::bytea"))
    async with pg_sessions() as session:
        stored_id = await session.scalar(select(RawApiResponse.id))
        _, errors = await verify_responses(session)
        guards = await verify_guards(session)
    assert errors == [f"stored Google response {stored_id} no longer matches its SHA-256"]
    assert any("raw_api_responses_append_only is disabled" in item for item in guards)


async def test_deletion_events_keep_the_timeline(pg_sessions) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=page_body(1))

    client = google(handler, pg_sessions)
    start = datetime(2026, 8, 17, 9, tzinfo=UTC)
    end = start + timedelta(hours=3)
    for _ in range(2):
        async with pg_sessions.begin() as session:
            await store_page(session, SPEC, await client.list_data_points(SPEC, start, end))
        async with pg_sessions.begin() as session:
            await mark_source_deletions(
                session, SPEC, start, end, datetime.now(UTC) + timedelta(seconds=1)
            )
    async with pg_sessions() as session:
        events = (
            await session.execute(
                select(RawHealthDataEvent.event, RawHealthDataEvent.previous_deleted_at).order_by(
                    RawHealthDataEvent.id
                )
            )
        ).all()
    assert [event for event, _ in events] == ["source-deleted", "source-returned", "source-deleted"]
    assert events[0][1] is None and events[1][1] is not None
