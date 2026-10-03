import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.google_health.oauth import AccessToken, TokenProvider


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
