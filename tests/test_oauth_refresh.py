import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import h11
import httpx
import pytest
from oauth_streams import TokenStream

from app.core.config import Settings
from app.google_health import oauth
from app.google_health.oauth import AccessToken, OAuthClient, OAuthError, TokenProvider


@pytest.mark.parametrize(
    "lifetime",
    [{}] + [{"expires_in": v} for v in (120, "120", 120.0, 3599.5, "3599.5", 1.5, " 7 ")],
)
@pytest.mark.parametrize("status", [200, 201, 299])
@pytest.mark.parametrize(
    "refresh_field",
    [{}, {"refresh_token": None}, {"refresh_token": ""}, {"refresh_token": " \töpaque \r\n"}],
)
async def test_valid_lifetime_and_access_token_are_preserved(
    monkeypatch, lifetime, status, refresh_field
):
    monkeypatch.setattr(oauth.time, "time", lambda: 1000.0)
    payload = {
        "access_token": " \tsynthetic access",
        **refresh_field,
        "metadata": [{"error": "nested"}, {"error": "sibling"}],
        "numbers": ["NaN", "Infinity", "-Infinity", 0, -12, 1.25, 1e3, 1e300],
        "scope": " synthetic-scope ",
        **lifetime,
    }
    body = json.dumps(payload).replace('"error": "sibling"', '"\\u0065rror": "sibling"')
    body = body.replace("1000.0", "1e3").replace("1e+300", "1e400").encode()
    payload["numbers"][-1] = float("inf")  # Standard JSON syntax keeps normal float decoding.
    hook = AsyncMock()
    streams = []

    def respond(request):
        stream = TokenStream([body[:10], body[10:]])
        streams.append(stream)
        return httpx.Response(
            302 if request.url.path == "/page" else status,
            headers={"Location": "/destination"},
            stream=stream,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        follow_redirects=True,
        event_hooks={"response": [hook]},
    ) as http:
        client = OAuthClient(Settings(_env_file=None), http)
        token = await client.refresh("synthetic-refresh")
        assert await client.exchange_code("synthetic-code") == payload
        assert (await http.get("https://example.test/page")).json() == payload
    assert hook.await_count == 4
    assert all(stream.closed.is_set() and stream.close_calls == 1 for stream in streams)
    assert token.value == payload["access_token"]
    assert token.replacement_refresh_token == (payload.get("refresh_token") or None)
    assert token.scopes == payload["scope"]
    # Fractional lifetimes are rounded down rather than dropping the connection.
    assert token.expires_at == 1000.0 + int(float(lifetime.get("expires_in", 3600)))


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("refresh_token", value, "a blank refresh")
        for value in (" ", "\t", "\r\n", " \t\r\n", "\u00a0\u2003")
    ]
    + [
        ("access_token", template.format(char), "an invalid access")
        for char in ("ö", "\U0001f9ec", "\ud800", "\udfff", "\r", "\n", "\0")
        + ("\x0b", "\x0c", "\x01", "\x1f", "\x7f")
        for template in ("{}synthetic-private", "synthetic{}-private", "synthetic-private{}")
    ]
    + [("access_token", f"synthetic-private{end}", "an invalid access") for end in (" ", "\t")],
)
async def test_invalid_token_field_is_rejected_without_retry(field, value, reason):
    handler = AsyncMock(
        return_value=httpx.Response(
            200,
            content=json.dumps(
                {
                    "access_token": "synthetic-access",
                    "refresh_token": "synthetic-refresh",
                    field: value,
                }
            ).encode(),
        )
    )
    sleep = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OAuthError, match=f"^OAuth response contained {reason} token$"):
            await OAuthClient(Settings(_env_file=None), http, sleep=sleep).refresh("synthetic")
    assert handler.await_count == 1
    sleep.assert_not_awaited()


async def test_invalidated_inflight_response_cannot_restore_old_access_or_refresh_token():
    entered, release = asyncio.Event(), asyncio.Event()
    stored = {"refresh": "A"}
    saved = []

    async def load():
        return stored["refresh"]

    async def save(value, scopes):
        saved.append(value)
        stored["refresh"] = value

    async def refresh(value):
        if value == "A":
            entered.set()
            await release.wait()
            return AccessToken("old-access", 99999999999, "rotated-A")
        assert value == "B"
        return AccessToken("new-access", 99999999999, "rotated-B")

    provider = TokenProvider(SimpleNamespace(refresh=refresh), load, save)
    pending = asyncio.create_task(provider.get())
    await asyncio.wait_for(entered.wait(), 2)
    stored["refresh"] = "B"
    provider.invalidate()
    release.set()
    assert await asyncio.wait_for(pending, 2) == "new-access"
    assert await provider.get() == "new-access"
    assert saved == ["rotated-B"]
    assert stored["refresh"] == "rotated-B"


async def test_rotation_storage_failure_does_not_publish_access_token():
    oauth = SimpleNamespace(
        refresh=AsyncMock(return_value=AccessToken("candidate", 99999999999, "rotated"))
    )
    loader = AsyncMock(return_value="old")
    saver = AsyncMock(side_effect=[RuntimeError("Synthetic storage failure"), None])
    provider = TokenProvider(oauth, loader, saver)
    with pytest.raises(RuntimeError, match="Synthetic storage failure"):
        await provider.get()
    assert provider._token is None
    assert await provider.get() == "candidate"
    assert oauth.refresh.await_count == 2


async def test_accepted_access_tokens_are_valid_h11_header_values():
    """Every token refresh accepts must pass h11's own Authorization validation."""
    tokens = [f"a{chr(c)}z" for c in range(128)] + [f"az{chr(c)}" for c in range(128)]
    tokens += ["\x0b", "\x0c", " leading", "\tleading", "inner \t space"]

    def respond(request):
        return httpx.Response(200, json={"access_token": tokens[len(seen)]})

    seen = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OAuthClient(Settings(_env_file=None), http)
        for token in tokens:
            try:
                accepted = (await client.refresh("synthetic")).value == token
            except OAuthError as exc:
                assert str(exc) in {
                    "OAuth response contained an invalid access token",
                    "OAuth response did not contain an access token",
                }
                accepted = False
            seen.append(token)
            header = [("Host", "x"), ("Authorization", f"Bearer {token}".encode())]
            try:
                h11.Request(method="GET", target="/", headers=header)
            except h11.LocalProtocolError:
                assert not accepted, repr(token)
            else:
                # Stricter than h11 only for controls and DEL (RFC 9110 field-vchar).
                assert accepted or any(ord(c) < 0x20 and c != "\t" or c == "\x7f" for c in token)


@pytest.mark.parametrize("grant", ["refresh", "exchange_code"])
async def test_deeply_nested_token_json_is_rejected_as_invalid(grant):
    handler = AsyncMock(return_value=httpx.Response(200, content=b"[" * 200_000))
    sleep = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OAuthError) as caught:
            await getattr(OAuthClient(Settings(_env_file=None), http, sleep=sleep), grant)("x")
    assert str(caught.value) == "OAuth token endpoint returned invalid JSON"
    assert not caught.value.transient and handler.await_count == 1
    assert caught.value.__cause__ is caught.value.__context__ is None
    sleep.assert_not_awaited()
