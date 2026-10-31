import asyncio
import logging
from unittest.mock import AsyncMock

import httpx
import pytest
from oauth_streams import PAYLOAD, TokenStream
from reconciliation_records import END, START

from app.core.config import Settings
from app.google_health import oauth
from app.google_health.client import GoogleHealthClient
from app.google_health.data_types import REGISTRY
from app.google_health.oauth import OAuthClient, OAuthError, TokenProvider
from app.sync.incremental import IncrementalSync
from app.sync.reconciliation import ReconciliationService


@pytest.fixture(autouse=True)
def enable_events(monkeypatch):
    # Alembic's fileConfig disables loggers created before the migration tests.
    for name in ("app.google_health.oauth", "app.sync.incremental", "app.sync.reconciliation"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)


@pytest.mark.parametrize("warmup", [False, True])
async def test_transport_retry_refreshes_once_without_failing_sync(sessions, caplog, warmup):
    attempts = 0

    def respond(request):
        nonlocal attempts
        if request.url.path == "/token":
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectError("synthetic private network details")
            return httpx.Response(200, json={"access_token": "synthetic-access"})
        assert request.headers["Authorization"] == "Bearer synthetic-access"
        return httpx.Response(200, json={"dataPoints": []})

    settings, sleep = Settings(_env_file=None), AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OAuthClient(settings, http, sleep=sleep)
        tokens = TokenProvider(client, AsyncMock(return_value="synthetic-refresh"))
        sync = IncrementalSync(
            settings,
            sessions,
            GoogleHealthClient(settings, tokens, http),
            {"heart-rate": REGISTRY["heart-rate"]},
        )
        with caplog.at_level(logging.INFO):
            if warmup:
                await ReconciliationService(sync).run(1)
            else:
                await sync.run("heart-rate", START, END)
    assert attempts == 2
    sleep.assert_awaited_once_with(1)
    events = [getattr(row, "event", None) for row in caplog.records]
    assert events.count("TOKEN_REFRESHED") == 1 and "SYNC_FAILED" not in events
    retry = next(
        row for row in caplog.records if getattr(row, "event", None) == "TOKEN_REFRESH_RETRY"
    )
    assert retry.attempt == 2
    assert "synthetic private" not in caplog.text


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_transient_http_response_is_retried(status):
    streams = [TokenStream([PAYLOAD]), TokenStream([b'{"access_token":"synthetic"}'])]
    responses = iter(
        [httpx.Response(status, stream=streams[0]), httpx.Response(200, stream=streams[1])]
    )
    sleep = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(responses))) as http:
        token = await OAuthClient(Settings(_env_file=None), http, sleep=sleep).refresh("synthetic")
    assert token.value == "synthetic"
    sleep.assert_awaited_once_with(1)
    assert streams[0].pulled == 0 and streams[1].pulled == 1
    assert all(stream.closed.is_set() and stream.close_calls == 1 for stream in streams)


@pytest.mark.parametrize("status", [100, 199, 301, 302, 303, 304, 307, 308, 400, 401, 403, 600])
@pytest.mark.parametrize("grant", ["refresh", "exchange_code"])
@pytest.mark.parametrize(
    "location",
    [None, "/private", "https://example.test/private", "https://example.test:bad/", "http://[bad"],
)
async def test_permanent_oauth_error_does_not_retry(status, grant, location, caplog):
    stream = TokenStream([PAYLOAD])
    respond = AsyncMock(
        return_value=httpx.Response(
            status, stream=stream, headers={"Location": location} if location else {}
        )
    )
    sleep = AsyncMock()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as http:
        settings = Settings(_env_file=None, google_client_secret="synthetic-private")
        client = OAuthClient(settings, http, sleep=sleep)
        http.event_hooks["response"] = [AsyncMock(side_effect=lambda r: r.raise_for_status())]
        with caplog.at_level(logging.INFO), pytest.raises(OAuthError) as failure:
            await getattr(client, grant)("synthetic")
    assert str(failure.value) == f"OAuth token endpoint returned HTTP {status}"
    assert failure.value.transient is False and failure.value.__cause__ is None
    assert respond.await_count == 1
    assert stream.pulled == 0 and stream.closed.is_set() and stream.close_calls == 1
    sleep.assert_not_awaited()
    assert not any(getattr(row, "event", None) for row in caplog.records)
    assert "private" not in repr([vars(row) for row in caplog.records])


@pytest.mark.parametrize("grant", ["refresh", "exchange_code"])
@pytest.mark.parametrize("status", [None, 429, 503])
async def test_retry_exhaustion_is_bounded_and_transient(status, grant, caplog):
    def respond(request):
        if status is None:
            raise httpx.ConnectError("synthetic private details")
        return httpx.Response(status)

    handler, sleep = AsyncMock(side_effect=respond), AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with caplog.at_level(logging.INFO), pytest.raises(OAuthError) as failure:
            client = OAuthClient(Settings(_env_file=None), http, sleep=sleep)
            await getattr(client, grant)("synthetic")
    # A single-use authorization code is never resent: the cause stays a network error.
    retried = grant == "refresh"
    assert failure.value.transient is True and handler.await_count == (4 if retried else 1)
    assert [call.args[0] for call in sleep.await_args_list] == ([1, 2, 4] if retried else [])
    retries = [r for r in caplog.records if getattr(r, "event", None) == "TOKEN_REFRESH_RETRY"]
    assert [row.attempt for row in retries] == ([2, 3, 4] if retried else [])
    assert "synthetic private" not in caplog.text


@pytest.mark.parametrize("phase", ["request", "backoff"])
async def test_total_deadline_includes_slow_requests_and_backoff(monkeypatch, phase):
    monkeypatch.setattr(oauth, "TOKEN_REQUEST_TIMEOUT_SECONDS", 0.01)
    cancelled = []

    async def slow_sleep(delay):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    handler = AsyncMock(
        side_effect=slow_sleep if phase == "request" else httpx.ConnectError("synthetic")
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OAuthError) as failure:
            await asyncio.wait_for(
                OAuthClient(Settings(_env_file=None), http, sleep=slow_sleep).refresh("synthetic"),
                1,
            )
    assert failure.value.transient and isinstance(failure.value.__cause__, TimeoutError)
    assert handler.await_count == 1 and cancelled == [True]


async def test_cancellation_during_backoff_propagates_and_releases_provider_lock():
    entered = asyncio.Event()

    async def wait(delay):
        entered.set()
        await asyncio.Event().wait()

    handler = AsyncMock(
        side_effect=[
            httpx.ConnectError("synthetic"),
            httpx.Response(200, json={"access_token": "synthetic"}),
        ]
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = TokenProvider(
            OAuthClient(Settings(_env_file=None), http, sleep=wait),
            AsyncMock(return_value="synthetic"),
        )
        task = asyncio.create_task(provider.get())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.wait_for(provider.get(), 2) == "synthetic"
