import asyncio
import json
import logging
import traceback
from datetime import UTC, datetime

import httpx
import pytest

from app.core.config import Settings
from app.google_health.client import GoogleHealthClient, GoogleHealthError, InvalidPageTokenError
from app.google_health.data_types import REGISTRY
from app.google_health.oauth import AccessToken, TokenProvider
from app.sync.errors import error_summary, is_transient


class Tokens:
    async def get(self):
        return "access"

    def invalidate(self):
        pass


async def _record(target, value):
    target.append(value)


async def _value(value):
    return value


@pytest.mark.parametrize(
    "status", [101, 199, 200, 206, 299, 300, 301, 302, 303, 304, 305, 307, 308, 399, 403]
)
@pytest.mark.parametrize("kind", ["steps", "floors", "total-calories"])
@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize(
    "location",
    [
        "https://example.invalid/private-marker",
        "https://example.invalid:bad/private-marker",
        "https://[invalid-ipv6]/private-marker",
    ],
)
async def test_only_success_statuses_produce_pages(status, kind, full, location):
    calls = []
    payload = (
        {
            "dataPoints": [{"name": "private-marker", "error": {}}],
            "rollupDataPoints": [{"name": "private-marker", "error": None}],
            **({"error": {"message": "private-marker"}} if status not in {200, 299} else {}),
        }
        if full
        else {}
    )

    def handler(request):
        calls.append(request)
        assert len(calls) == 1
        return httpx.Response(status, json=payload, headers={"Location": location})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as http:
        client = GoogleHealthClient(Settings(_env_file=None), Tokens(), http)
        start = datetime(2026, 8, 1, tzinfo=UTC)
        if 200 <= status < 300 and status != 206:
            page = await client.list_data_points(REGISTRY[kind], start, start)
            assert len(page.data_points) == int(full) and page.next_page_token is None
            expected = None if kind == "total-calories" else {}
            assert all(p["error"] == expected for p in page.data_points)
        else:
            with pytest.raises(GoogleHealthError) as caught:
                await client.list_data_points(REGISTRY[kind], start, start)
            assert str(caught.value) == f"Google Health API returned HTTP {status}"
            assert error_summary(caught.value) == f"GoogleHealthError (http_{status})"
            assert not is_transient(caught.value)
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["steps", "floors", "total-calories"])
@pytest.mark.parametrize(
    "value",
    [None, [], {}, False, "private-marker HTTP 503 INVALID_PAGE_TOKEN", [{"private-marker": 42}]],
)
@pytest.mark.parametrize("expected", ["missing", "empty", "full", "invalid"])
async def test_collection_field_presence_and_expected_list_type(kind, value, expected, caplog):
    key, other = "dataPoints", "rollupDataPoints"
    if kind == "total-calories":
        key, other = other, key
    payload = {other: value}
    points = [{"name": "expected-record"}] if expected == "full" else []
    if expected != "missing":
        payload[key] = None if expected == "invalid" else points
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GoogleHealthClient(
            Settings(_env_file=None, google_api_max_retries=4), Tokens(), http
        )
        start = datetime(2026, 8, 1, tzinfo=UTC)
        if expected in {"missing", "invalid"}:
            with pytest.raises(GoogleHealthError) as caught:
                await client.list_data_points(REGISTRY[kind], start, start, "saved")
            message = (
                "Google Health response has an unexpected data point collection"
                if expected == "missing"
                else "Google Health response has an invalid data point list"
            )
            assert str(caught.value) == message
            assert error_summary(caught.value).endswith(" (invalid_response)")
            assert not is_transient(caught.value)
            assert caught.value.__cause__ is caught.value.__context__ is None
            assert "private-marker" not in str(caught.value) + caplog.text
        else:
            page = await client.list_data_points(REGISTRY[kind], start, start, "saved")
            assert page.data_points == points and page.next_page_token is None
    assert len(calls) == 1


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize(
    ("header", "expected"),
    [("0", 0), ("-2", 0), ("2.5", 2.5), ("60", 60)]
    + [(v, 1.25) for v in (None, "", "inf", "-inf", "1e309", "NaN", "bad", "1e999999")]
    + [
        ("Wed, 28 Oct 2026 12:00:30 GMT", 30),
        ("Wed, 28 Oct 2026 12:01:00 GMT", 60),
        ("Wed, 28 Oct 2026 11:59:00 GMT", 0),
        ("Wed, 28 Oct 2026 12:00:30", 1.25),
        ("Fri, 31 Dec 999999999999 23:59:59 GMT", 1.25),
    ],
)
async def test_google_api_retries_rate_limit(monkeypatch, clock, status, header, expected):
    import app.google_health.client as module
    from app.services import storage

    clock.now = datetime(2026, 10, 28, 12, tzinfo=UTC)
    monkeypatch.setattr(module, "datetime", storage.datetime)
    monkeypatch.setattr(module.random, "random", lambda: 0.25)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(status, headers={"Retry-After": header} if header else {})
        return httpx.Response(200, json={"healthUserId": "1"})

    sleeps = []
    client = GoogleHealthClient(
        Settings(
            _env_file=None, google_api_max_retries=2, google_api_min_request_interval_seconds=0
        ),
        Tokens(),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=lambda delay: _record(sleeps, delay),
    )
    assert (await client.get_identity())["healthUserId"] == "1"
    assert calls == 2
    assert sleeps == [expected]


@pytest.mark.parametrize("phase", ["parse", "subtract"])
@pytest.mark.parametrize("error", [TypeError, ValueError, OverflowError, OSError])
def test_retry_after_date_failures_use_backoff(monkeypatch, phase, error):
    import app.google_health.client as module

    class BadDate:
        def __sub__(self, other):
            raise error("private-header")

    def parse(header):
        if phase == "parse":
            raise error("private-header")
        return BadDate()

    monkeypatch.setattr(module, "parsedate_to_datetime", parse)
    monkeypatch.setattr(module.random, "random", lambda: 0.25)
    response = httpx.Response(503, headers={"Retry-After": "private-header"})
    assert [module._retry_delay(response, n) for n in (0, 2, 6)] == [1.25, 4.25, 60]


async def test_google_api_retries_transport_failure():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("temporary", request=request)
        return httpx.Response(200, json={"healthUserId": "1"})

    client = GoogleHealthClient(
        Settings(google_api_max_retries=2),
        Tokens(),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=lambda delay: _value(None),
    )
    assert (await client.get_identity())["healthUserId"] == "1"
    assert calls == 2


async def test_401_refresh_is_independent_of_retry_budget():
    class RefreshingTokens(Tokens):
        invalidations = 0

        def invalidate(self):
            self.invalidations += 1

    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(401 if calls == 1 else 200, json={"healthUserId": "1"})

    tokens = RefreshingTokens()
    client = GoogleHealthClient(
        Settings(google_api_max_retries=0),
        tokens,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    assert (await client.get_identity())["healthUserId"] == "1"
    assert tokens.invalidations == 1
    assert calls == 2


@pytest.mark.parametrize("status", [200, 206, 401])
async def test_candidate_identity_never_uses_or_refreshes_active_credentials(status):
    class ActiveTokens:
        async def get(self):
            raise AssertionError("Candidate identity must not read the active token")

        def invalidate(self):
            raise AssertionError("Candidate identity must not invalidate the active token")

    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer candidate-access"
        return httpx.Response(status, json={"healthUserId": "candidate-account"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GoogleHealthClient(Settings(_env_file=None), ActiveTokens(), http)
        if status == 200:
            assert (await client.get_identity(access_token="candidate-access"))["healthUserId"] == (
                "candidate-account"
            )
        else:
            with pytest.raises(GoogleHealthError, match=f"HTTP {status}"):
                await client.get_identity(access_token="candidate-access")
    assert len(calls) == 1


async def test_non_object_api_json_is_rejected():
    client = GoogleHealthClient(
        Settings(),
        Tokens(),
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
        ),
    )

    try:
        await client.get_identity()
    except RuntimeError as exc:
        assert "non-object" in str(exc)
    else:
        raise AssertionError("non-object API JSON was accepted")


async def test_oauth_token_provider_refreshes_once():
    class OAuth:
        calls = 0

        async def refresh(self, refresh_token):
            self.calls += 1
            assert refresh_token == "refresh"
            return AccessToken("new-access", 99999999999)

    oauth = OAuth()
    provider = TokenProvider(oauth, lambda: _value("refresh"))
    assert await provider.get() == "new-access"
    assert await provider.get() == "new-access"
    assert oauth.calls == 1


async def test_oauth_token_provider_persists_rotated_refresh_token():
    class OAuth:
        async def refresh(self, refresh_token):
            assert refresh_token == "old-refresh"
            return AccessToken(
                "new-access",
                99999999999,
                replacement_refresh_token="new-refresh",
                scopes="scope-a scope-b",
            )

    saved = []

    async def save(refresh_token, scopes):
        saved.append((refresh_token, scopes))

    provider = TokenProvider(OAuth(), lambda: _value("old-refresh"), save)

    assert await provider.get() == "new-access"
    assert saved == [("new-refresh", "scope-a scope-b")]


async def test_rollup_only_type_uses_post_and_preserves_points():
    def handler(request):
        assert request.url.path.endswith("/total-calories/dataPoints:rollUp")
        assert request.method == "POST"
        assert b'"windowSize":"60s"' in request.content
        body = json.loads(request.content)
        assert body["range"] == {
            "startTime": "2026-08-01T00:00:00Z",
            "endTime": "2026-08-02T00:01:00Z",
        }
        return httpx.Response(
            200,
            json={
                "rollupDataPoints": [
                    {
                        "startTime": "2026-08-01T00:00:00Z",
                        "endTime": "2026-08-01T00:01:00Z",
                        "totalCalories": {"kcal": 1.5},
                    }
                ]
            },
        )

    client = GoogleHealthClient(
        Settings(), Tokens(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    page = await client.list_data_points(
        REGISTRY["total-calories"],
        datetime(2026, 8, 1, 0, 0, 37, 123, tzinfo=UTC),
        datetime(2026, 8, 2, 0, 0, 12, 456, tzinfo=UTC),
    )
    assert page.data_points[0]["totalCalories"]["kcal"] == 1.5


async def test_reconcile_only_type_uses_reconciled_stream():
    def handler(request):
        assert request.url.path.endswith("/floors/dataPoints:reconcile")
        assert request.url.params["dataSourceFamily"].endswith("/all-sources")
        return httpx.Response(200, json={"dataPoints": []})

    client = GoogleHealthClient(
        Settings(), Tokens(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    page = await client.list_data_points(
        REGISTRY["floors"],
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 8, 2, tzinfo=UTC),
    )
    assert page.data_points == []


async def test_invalid_page_token_has_typed_error():
    def handler(request):
        return httpx.Response(
            400,
            json={
                "error": {
                    "status": "INVALID_ARGUMENT",
                    "details": [{"reason": "INVALID_PAGE_TOKEN"}],
                }
            },
        )

    client = GoogleHealthClient(
        Settings(), Tokens(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    try:
        await client.list_data_points(
            REGISTRY["steps"],
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 8, 2, tzinfo=UTC),
            "expired",
        )
    except InvalidPageTokenError:
        pass
    else:
        raise AssertionError("invalid page token was not surfaced")


async def test_reference_catalog_list_omits_unsupported_time_filter():
    def handler(request):
        assert request.url.path.endswith("/food/dataPoints")
        assert "filter" not in request.url.params
        return httpx.Response(200, json={"dataPoints": []})

    client = GoogleHealthClient(
        Settings(), Tokens(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    await client.list_data_points(
        REGISTRY["food"],
        datetime(2010, 1, 1, tzinfo=UTC),
        datetime(2026, 8, 18, tzinfo=UTC),
    )


@pytest.mark.parametrize("kind", ["steps", "floors", "total-calories"])
@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("null", None), ('""', None), ('" opaque \\t"', " opaque \t")]
    + [
        (value, GoogleHealthError)
        for value in "false true 0 1 -1 1.5 [] {}".split()
        + [
            '["INVALID_PAGE_TOKEN"]',
            '{"private-marker": "HTTP 503 INVALID_PAGE_TOKEN"}',
        ]
    ],
)
async def test_next_page_token_requires_string_or_terminal_form(kind, raw, expected):
    payload = {} if raw is None else {"nextPageToken": json.loads(raw)}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as http:
        client = GoogleHealthClient(Settings(), Tokens(), http)
        start = datetime(2026, 8, 1, tzinfo=UTC)
        if expected is GoogleHealthError:
            with pytest.raises(GoogleHealthError) as caught:
                await client.list_data_points(REGISTRY[kind], start, start)
            assert (
                str(caught.value) == "Google Health response has an invalid pagination token type"
            )
            assert (
                error_summary(caught.value) == "InvalidPaginationResponseError (invalid_response)"
            )
            assert not is_transient(caught.value)
        else:
            page = await client.list_data_points(REGISTRY[kind], start, start)
            assert page.next_page_token == expected


@pytest.mark.parametrize("end", [" ", "\t", "\x0b", "\x0c"])
async def test_header_rejected_by_h11_is_not_retried_or_quoted(end, caplog):
    """MockTransport skips header validation: send through a real loopback h11 stack."""
    token = "synthetic-private-token" + end
    connections = []

    async def accept(reader, writer):
        connections.append(writer)
        await reader.read()
        writer.close()

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    settings = Settings(
        _env_file=None,
        google_health_base_url=f"http://127.0.0.1:{port}",
        google_api_min_request_interval_seconds=0,
        google_api_max_retries=3,
    )
    sleeps = []
    async with server, httpx.AsyncClient() as http:
        google = GoogleHealthClient(settings, Tokens(), http, sleep=lambda d: _record(sleeps, d))
        with pytest.raises(GoogleHealthError) as caught:
            await google.get_identity(access_token=token)
        logging.getLogger("synthetic.backfill").exception("failed", exc_info=caught.value)
    assert str(caught.value) == "Google Health request was rejected before sending"
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert not is_transient(caught.value) and sleeps == [] and len(connections) <= 1
    rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert "synthetic-private" not in rendered and "Illegal header" not in rendered


@pytest.mark.parametrize("opening", [b"[", b'{"a":'])
async def test_deeply_nested_json_is_invalid_json_not_a_crash(opening):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=opening * 200_000)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        google = GoogleHealthClient(
            Settings(_env_file=None, google_api_min_request_interval_seconds=0),
            Tokens(),
            http,
        )
        with pytest.raises(GoogleHealthError) as caught:
            await google.list_data_points(REGISTRY["steps"], datetime.now(UTC), datetime.now(UTC))
    assert error_summary(caught.value) == "GoogleHealthError (invalid_json)"
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert not is_transient(caught.value) and len(calls) == 1
