import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import delete, select

from app.api import auth
from app.core.config import Settings
from app.core.host_validation import HostValidationMiddleware
from app.google_health.client import GoogleHealthError
from app.google_health.oauth import AccessToken, TokenProvider
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


async def callback(application):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application, raise_app_exceptions=False),
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


async def test_without_new_refresh_token_reuses_only_verified_matching_account(linked, sessions):
    runtime = linked.state.runtime
    runtime.oauth.exchange_code.return_value = {"access_token": "candidate-access-A"}
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
