from datetime import UTC, datetime

import pytest

from app.google_health.client import InvalidPageTokenError, Page
from app.google_health.data_types import REGISTRY
from app.google_health.pagination import iter_pages


class FakeClient:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.tokens = []

    async def list_data_points(self, spec, start, end, token):
        self.tokens.append(token)
        return next(self.pages)


@pytest.mark.asyncio
async def test_pagination_follows_every_token():
    client = FakeClient([Page([{"a": 1}], "next"), Page([{"a": 2}], None)])
    now = datetime.now(UTC)
    pages = [page async for page in iter_pages(client, REGISTRY["steps"], now, now)]
    assert len(pages) == 2
    assert client.tokens == [None, "next"]


@pytest.mark.asyncio
async def test_pagination_rejects_repeated_token():
    client = FakeClient([Page([], "same"), Page([], "same")])
    now = datetime.now(UTC)
    with pytest.raises(RuntimeError, match="repeated"):
        _ = [page async for page in iter_pages(client, REGISTRY["steps"], now, now)]


@pytest.mark.asyncio
async def test_pagination_restarts_window_after_expired_checkpoint():
    class ExpiredClient:
        tokens = []

        async def list_data_points(self, spec, start, end, token):
            self.tokens.append(token)
            if token == "expired":
                raise InvalidPageTokenError("expired")
            return Page([{"ok": True}], None)

    client = ExpiredClient()
    now = datetime.now(UTC)
    pages = [
        page async for page in iter_pages(client, REGISTRY["steps"], now, now, page_token="expired")
    ]
    assert client.tokens == ["expired", None]
    assert pages[0].data_points == [{"ok": True}]
