import json
from datetime import UTC, datetime

import httpx
import pytest

from app.core.config import Settings
from app.google_health.client import GoogleHealthClient, GoogleHealthError, InvalidPageTokenError
from app.google_health.data_types import REGISTRY
from app.google_health.oauth import AccessToken, TokenProvider


class Tokens:
    async def get(self):
        return "access"

    def invalidate(self):
        pass


async def _record(target, value):
    target.append(value)


async def _value(value):
    return value


async def test_google_api_retries_rate_limit():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"healthUserId": "1"})

    sleeps = []
    client = GoogleHealthClient(
        Settings(google_api_max_retries=2),
        Tokens(),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=lambda delay: _record(sleeps, delay),
    )
    assert (await client.get_identity())["healthUserId"] == "1"
    assert calls == 2
    assert sleeps == [0.0]


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


@pytest.mark.parametrize("status", [200, 401])
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
            with pytest.raises(GoogleHealthError, match="HTTP 401"):
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
