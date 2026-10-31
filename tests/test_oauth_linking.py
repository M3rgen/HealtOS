import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import time
import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from compressed_records import compressed
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from oauth_streams import LIMIT, TokenStream
from sqlalchemy import delete, func, select

from app.api import auth
from app.core.config import Settings
from app.core.host_validation import HostValidationMiddleware
from app.google_health import oauth
from app.google_health.client import GoogleHealthError
from app.google_health.oauth import AccessToken, OAuthClient, OAuthError, TokenProvider
from app.models.raw_versions import RawApiResponse
from app.models.state import OAuthToken
from app.services.credentials import CredentialStore


@pytest_asyncio.fixture
async def linked(sessions):
    settings = Settings(_env_file=None, google_token_encryption_key=Fernet.generate_key().decode())
    store = CredentialStore(settings, sessions)
    await store.save_connection("refresh-A", "scope-A", "account-A", "legacy-A")
    oauth = SimpleNamespace(
        exchange_code=AsyncMock(
            return_value={
                "access_token": "candidate-access-B",
                "refresh_token": "refresh-B",
                "scope": "scope-B",
            }
        ),
        refresh=AsyncMock(return_value=AccessToken("active-access-A", 99999999999)),
    )
    tokens = TokenProvider(oauth, store.load_refresh_token, store.save_refresh_token)
    tokens._token = AccessToken("cached-access-A", 99999999999)
    google = SimpleNamespace(
        get_identity=AsyncMock(
            return_value={"healthUserId": "account-B", "legacyUserId": "legacy-B"}
        )
    )
    application = FastAPI()
    application.add_middleware(HostValidationMiddleware)
    application.include_router(auth.router)
    application.state.settings = settings
    application.state.runtime = SimpleNamespace(
        credentials=store, tokens=tokens, oauth=oauth, google=google
    )
    return application


async def callback(application, *, raise_app_exceptions=False):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application, raise_app_exceptions=raise_app_exceptions),
        base_url="http://localhost:8000",
    ) as client:
        return await client.get(
            "/auth/google/callback",
            params={"code": "synthetic-code", "state": auth._state(application.state.settings)},
        )


async def connection(sessions):
    async with sessions() as session:
        row = await session.scalar(select(OAuthToken).where(OAuthToken.subject == "me"))
        return row.encrypted_refresh_token, row.scopes, row.health_user_id, row.legacy_user_id


@pytest.mark.parametrize(
    "invalid",
    [{"access_token": value} for value in ("", " \t\r\n", None, 123)]
    + [
        {"access_token": template.format(char)}
        for char in ("ö", "\U0001f9ec", "\ud800", "\udfff", "\r", "\n", "\0")
        for template in ("{}synthetic-private", "synthetic{}-private", "synthetic-private{}")
    ]
    + [{"refresh_token": value} for value in (" ", "\t", "\r\n", " \t\r\n", "\u00a0\u2003")]
    + [
        {"refresh_token": value}
        for value in (True, False, 0, 17, 1.25, [], ["synthetic-private"], {}, {"private": 1})
    ]
    + [
        {"expires_in": value}
        for value in [True, False, 0.5, 0, -1, "0", "-1", "0.9", None, [], {}]
        + [float("inf"), float("-inf"), float("nan"), 10**400, str(10**400), 10**308]
        + ["synthetic-private-lifetime"]
    ]
    + [
        {"error": value}
        for value in [{"code": 429, "message": "synthetic-private-error"}, None, False, 0, "", []]
        + [f"{code} synthetic-private-error" for code in ("HTTP 429", "503", "INVALID_PAGE_TOKEN")]
    ]
    + [{"status": status} for status in (301, 302, 303, 304, 307, 308)]
    + [
        {"status": 206, "range": value}
        for value in (None, "bytes 0-9/100", "synthetic-private-range")
    ]
    + [{"wire_size": True}]
    + [{"decoded_size": encoding} for encoding in ("gzip", "deflate")]
    + [
        {"constant": (constant, location, "identity")}
        for constant in ("NaN", "Infinity", "-Infinity")
        for location in ("root", "top", "object", "array")
    ]
    + [
        {"constant": ("NaN", "array", encoding)}
        for encoding in ("gzip", "deflate", "raw-deflate", "gzip-members", "stacked")
    ]
    + [
        {"duplicate": (key, equal, escaped, nested)}
        for key in ("access_token", "refresh_token", "expires_in", "scope", "synthetic-private-key")
        for equal in (False, True)
        for escaped in (False, True)
        for nested in (False, True)
    ],
)
async def test_invalid_tokens_preserve_connection_then_recover(
    linked, sessions, caplog, monkeypatch, invalid
):
    runtime = linked.state.runtime
    before = await connection(sessions)
    status = invalid.get("status", 200)
    payload = {
        "access_token": "synthetic-private-access",
        "refresh_token": "synthetic-private-refresh",
        "scope": "rejected-scope",
        "expires_in": 3600,
        **invalid,
    }
    body = json.dumps(payload).encode()
    headers = {}
    if invalid.get("range") is not None:
        headers["Content-Range"] = invalid["range"]
    nonstandard = "constant" in invalid or (
        isinstance(invalid.get("expires_in"), float) and not math.isfinite(invalid["expires_in"])
    )
    if "constant" in invalid:
        constant, location, encoding = payload.pop("constant")
        value = "synthetic-private-placeholder"
        value = {"synthetic-private-field": value} if location == "object" else value
        value = [{"synthetic-private-field": [value]}] if location == "array" else value
        payload["synthetic-private-metadata"] = value
        body = json.dumps(payload).replace('"synthetic-private-placeholder"', constant).encode()
        body = constant.encode() if location == "root" else body
        headers["content-encoding"] = encoding.removeprefix("raw-")
        if encoding == "gzip-members":
            body = compressed(body[:20], "gzip") + compressed(body[20:], "gzip")
            headers["content-encoding"] = "gzip"
        elif encoding == "stacked":
            body = compressed(compressed(body, "deflate"), "gzip")
            headers["content-encoding"] = "deflate, gzip"
        else:
            body = compressed(body, encoding)
    if "wire_size" in invalid or "decoded_size" in invalid:
        body += b" " * LIMIT
        if "decoded_size" in invalid:
            headers["content-encoding"] = invalid["decoded_size"]
            body = compressed(body, invalid["decoded_size"])
    if "duplicate" in invalid:
        key, equal, escaped, nested = payload.pop("duplicate")
        value = payload.get(key, "synthetic-private-value")
        if not nested:
            payload.pop(key, None)
        name = f"\\u{ord(key[0]):04x}{key[1:]}" if escaped else key
        other = value if equal else (7200 if key == "expires_in" else "synthetic-private-other")
        pair = f'{json.dumps(key)}:{json.dumps(value)},"{name}":{json.dumps(other)}'
        fragment = '"metadata":[{' + pair + "}]" if nested else pair
        body = (json.dumps(payload)[:-1] + "," + fragment + "}").encode()
    recovered = dict(
        access_token="recovered-access", refresh_token=" \töpaque \r\n", scope="new-scope"
    )
    streams = [TokenStream([body[:10], body[10:]])]
    handler = AsyncMock(
        side_effect=[
            httpx.Response(status, stream=streams[0], headers=headers),
            httpx.Response(200, json=recovered),
        ]
    )
    sleep = AsyncMock()
    saver = AsyncMock(wraps=runtime.tokens.refresh_token_saver)
    runtime.tokens.refresh_token_saver = saver
    connection_saver = AsyncMock(wraps=runtime.credentials.save_connection)
    monkeypatch.setattr(runtime.credentials, "save_connection", connection_saver)
    oversized = "wire_size" in invalid or "decoded_size" in invalid
    both_grants = (
        status == 206
        or oversized
        or nonstandard
        or bool(invalid.keys() & {"access_token", "refresh_token", "error", "duplicate"})
    )
    monkeypatch.setattr(logging.getLogger("app.google_health.oauth"), "disabled", False)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as http:
        runtime.tokens.oauth = OAuthClient(linked.state.settings, http, sleep=sleep)
        runtime.tokens._token.expires_at = 0
        cached, baseline = runtime.tokens._token, asyncio.all_tasks()
        generation = runtime.tokens._generation
        with monkeypatch.context() as clock:
            if invalid.get("expires_in") == 10**308:
                clock.setattr(oauth, "time", SimpleNamespace(time=lambda: 1e308))
            with caplog.at_level(logging.INFO), pytest.raises(OAuthError) as failure:
                await runtime.tokens.get()
        expected = (
            f"OAuth token endpoint returned HTTP {status}"
            if status != 200
            else "OAuth token response exceeded its byte budget"
            if oversized
            else "OAuth token endpoint returned non-standard JSON constants"
            if nonstandard
            else "OAuth token endpoint returned duplicate JSON object keys"
            if "duplicate" in invalid
            else "OAuth response contained an invalid access token"
            if isinstance(invalid.get("access_token"), str) and invalid["access_token"].strip()
            else "OAuth response did not contain an access token"
            if "access_token" in invalid
            else "OAuth response contained an invalid refresh token"
            if "refresh_token" in invalid and not isinstance(invalid["refresh_token"], str)
            else "OAuth response contained a blank refresh token"
            if "refresh_token" in invalid
            else "OAuth token endpoint returned an error response"
            if "error" in invalid
            else "OAuth response contained an invalid token lifetime"
        )
        assert str(failure.value) == expected and not failure.value.transient
        assert handler.await_count == 1
        sleep.assert_not_awaited()
        saver.assert_not_awaited()
        if both_grants:
            assert failure.value.__cause__ is failure.value.__context__ is None
            runtime.oauth = runtime.tokens.oauth
            streams.append(TokenStream([body[:10], body[10:]]))
            handler.side_effect = [
                httpx.Response(status, stream=streams[-1], headers=headers),
                httpx.Response(200, json=recovered),
            ]
            with caplog.at_level(logging.INFO), pytest.raises(OAuthError) as exchange_failure:
                await callback(linked, raise_app_exceptions=True)
            assert str(exchange_failure.value) == expected and not exchange_failure.value.transient
            assert exchange_failure.value.__cause__ is exchange_failure.value.__context__ is None
            assert "synthetic-private" not in "".join(
                traceback.format_exception(exchange_failure.value)
            )
            runtime.google.get_identity.assert_not_awaited()
            assert handler.await_count == 2
        assert runtime.tokens._token is cached
        assert runtime.tokens._generation == generation
        assert cached == AccessToken("cached-access-A", 0)
        assert all(stream.closed.is_set() and stream.close_calls == 1 for stream in streams)
        if nonstandard or status == 206:
            assert not (asyncio.all_tasks() - baseline)
        if status == 206:
            assert all(stream.pulled == 0 for stream in streams)
        connection_saver.assert_not_awaited()
        saver.assert_not_awaited()
        assert runtime.tokens._token.value == "cached-access-A"
        assert await connection(sessions) == before
        assert await runtime.credentials.load_refresh_token() == "refresh-A"
        assert not any(getattr(row, "event", None) for row in caplog.records)
        assert all(
            marker not in "".join(traceback.format_exception(failure.value)) + caplog.text
            for marker in ("synthetic-private", "rejected-scope", repr(body))
        )
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(RawApiResponse)) == 0
        with caplog.at_level(logging.INFO):
            assert await asyncio.wait_for(runtime.tokens.get(), 2) == "recovered-access"
        assert await runtime.tokens.get() == "recovered-access"
        if both_grants:
            handler.side_effect = [httpx.Response(200, json=recovered)]
            assert (await callback(linked)).status_code == 200
            runtime.google.get_identity.assert_awaited_once_with(access_token="recovered-access")
            assert runtime.tokens._token is None
        sleep.assert_not_awaited()
    assert handler.await_count == (4 if both_grants else 2)
    assert await runtime.credentials.load_refresh_token() == recovered["refresh_token"]
    account = ("account-B", "legacy-B") if both_grants else ("account-A", "legacy-A")
    assert (await connection(sessions))[1:] == ("new-scope", *account)
    assert [getattr(row, "event", None) for row in caplog.records].count("TOKEN_REFRESHED") == 1
    assert "synthetic-private" not in repr([vars(row) for row in caplog.records])


@pytest.mark.parametrize("status", [200, 301, 302, 303, 304, 307, 308])
@pytest.mark.parametrize("access", ["", " \t\r\n"])
async def test_blank_exchange_access_does_not_activate_connection(
    linked, sessions, access, status, caplog
):
    runtime = linked.state.runtime
    before = await connection(sessions)
    payload = dict(access_token=access, refresh_token="synthetic-rejected-refresh")
    if status != 200:
        payload["access_token"] = "candidate-access-B"
    handler = AsyncMock(
        return_value=httpx.Response(status, json=payload, headers={"Location": "/private"})
    )
    sleep = AsyncMock()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as http:
        runtime.oauth = OAuthClient(linked.state.settings, http, sleep=sleep)
        with caplog.at_level(logging.INFO):
            assert (await callback(linked)).status_code == 500
        assert not any(getattr(row, "event", None) for row in caplog.records)
        assert "private" not in repr([vars(row) for row in caplog.records])
    assert handler.await_count == 1
    sleep.assert_not_awaited()
    runtime.google.get_identity.assert_not_awaited()
    assert await connection(sessions) == before
    assert await runtime.tokens.get() == "cached-access-A"


async def test_failed_identity_keeps_old_connection_and_cached_token(linked, sessions):
    runtime = linked.state.runtime
    before = await connection(sessions)
    runtime.google.get_identity.side_effect = GoogleHealthError("Synthetic identity failure")

    assert (await callback(linked)).status_code == 500
    assert await connection(sessions) == before
    assert await runtime.credentials.load_refresh_token() == "refresh-A"
    assert await runtime.tokens.get() == "cached-access-A"
    runtime.google.get_identity.assert_awaited_once_with(access_token="candidate-access-B")
    runtime.oauth.refresh.assert_not_awaited()


@pytest.mark.parametrize("identity", [{}, {"healthUserId": ""}, {"healthUserId": 123}])
async def test_invalid_identity_does_not_activate_candidate(linked, sessions, identity):
    before = await connection(sessions)
    linked.state.runtime.google.get_identity.return_value = identity
    assert (await callback(linked)).status_code == 502
    assert await connection(sessions) == before
    assert await linked.state.runtime.tokens.get() == "cached-access-A"


async def test_success_commits_candidate_token_and_its_identity(linked, sessions):
    runtime = linked.state.runtime
    response = await callback(linked)
    assert response.status_code == 200
    assert response.json() == {"status": "connected", "health_user_id": "account-B"}
    assert await runtime.credentials.load_refresh_token() == "refresh-B"
    assert (await connection(sessions))[1:] == ("scope-B", "account-B", "legacy-B")
    assert runtime.tokens._token is None
    runtime.google.get_identity.assert_awaited_once_with(access_token="candidate-access-B")
    runtime.oauth.refresh.assert_not_awaited()


async def test_commit_failure_keeps_old_connection_and_cache(linked, sessions):
    before = await connection(sessions)
    async with sessions.begin() as session:
        session.add(
            OAuthToken(subject="other", encrypted_refresh_token="synthetic", health_user_id="taken")
        )
    linked.state.runtime.google.get_identity.return_value = {"healthUserId": "taken"}
    assert (await callback(linked)).status_code == 500
    assert await connection(sessions) == before
    assert await linked.state.runtime.tokens.get() == "cached-access-A"


@pytest.mark.parametrize("refresh_field", [{}, {"refresh_token": None}, {"refresh_token": ""}])
async def test_without_new_refresh_token_reuses_only_verified_matching_account(
    linked, sessions, refresh_field
):
    runtime = linked.state.runtime
    runtime.oauth.exchange_code.return_value = dict(
        access_token="candidate-access-A", **refresh_field
    )
    runtime.google.get_identity.side_effect = [
        {"healthUserId": "account-A", "legacyUserId": "new-legacy-A"},
        {"healthUserId": "account-A"},
    ]
    runtime.oauth.refresh.return_value = AccessToken(
        "verified-access-A", 99999999999, "rotated-refresh-A", "updated-scope-A"
    )
    assert (await callback(linked)).status_code == 200
    runtime.oauth.refresh.assert_awaited_once_with("refresh-A")
    assert runtime.google.get_identity.await_args_list[1].kwargs == {
        "access_token": "verified-access-A"
    }
    assert await runtime.credentials.load_refresh_token() == "rotated-refresh-A"
    assert (await connection(sessions))[1:] == ("updated-scope-A", "account-A", "new-legacy-A")


async def test_missing_refresh_cannot_pair_account_B_identity_with_account_A_token(
    linked, sessions
):
    runtime = linked.state.runtime
    before = await connection(sessions)
    runtime.oauth.exchange_code.return_value = {"access_token": "candidate-access-B"}
    runtime.google.get_identity.side_effect = [
        {"healthUserId": "account-B"},
        {"healthUserId": "account-A"},
    ]
    assert (await callback(linked)).status_code == 400
    assert await connection(sessions) == before
    assert await runtime.tokens.get() == "cached-access-A"


async def test_first_link_requires_offline_token(linked, sessions):
    async with sessions.begin() as session:
        await session.execute(delete(OAuthToken))
    linked.state.runtime.oauth.exchange_code.return_value = {"access_token": "candidate-access-B"}
    assert (await callback(linked)).status_code == 400
    assert await linked.state.runtime.credentials.load_refresh_token() is None


async def test_callback_and_inflight_refresh_leave_new_connection_active(linked, sessions):
    runtime = linked.state.runtime
    runtime.tokens.invalidate()
    entered, release, identity_checked = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def refresh(value):
        if value == "refresh-A":
            entered.set()
            await release.wait()
            return AccessToken("old-access-A", 99999999999, "old-rotated-refresh-A")
        assert value == "refresh-B"
        return AccessToken("new-access-B", 99999999999)

    async def identity(*, access_token):
        assert access_token == "candidate-access-B"
        identity_checked.set()
        return {"healthUserId": "account-B", "legacyUserId": "legacy-B"}

    runtime.oauth.refresh.side_effect = refresh
    runtime.google.get_identity.side_effect = identity
    pending = asyncio.create_task(runtime.tokens.get())
    await asyncio.wait_for(entered.wait(), 2)
    updating = asyncio.create_task(callback(linked))
    try:
        await asyncio.wait_for(identity_checked.wait(), 2)
        await asyncio.sleep(0)
        assert not updating.done()
        assert await runtime.credentials.load_refresh_token() == "refresh-A"
    finally:
        release.set()
        old_access, response = await asyncio.wait_for(asyncio.gather(pending, updating), 2)
    assert old_access == "old-access-A"  # This read completed before the new connection committed.
    assert response.status_code == 200
    assert await runtime.credentials.load_refresh_token() == "refresh-B"
    assert (await connection(sessions))[1:] == ("scope-B", "account-B", "legacy-B")
    assert await runtime.tokens.get() == "new-access-B"
    assert await runtime.credentials.load_refresh_token() == "refresh-B"


def test_oauth_state_signed_with_an_empty_key_is_rejected():
    """An unset key may arrive as "" from the environment; it must not verify anything."""
    payload = f"{int(time.time())}.nonce"
    signature = hmac.new(b"", payload.encode(), hashlib.sha256).digest()
    forged = f"{payload}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"
    settings = Settings(_env_file=None, google_token_encryption_key="")
    with pytest.raises(HTTPException) as rejected:
        auth._verify_state(settings, forged)
    assert rejected.value.status_code == 400
