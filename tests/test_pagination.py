from datetime import UTC, datetime

import pytest

from app.google_health import pagination
from app.google_health.client import InvalidPageTokenError, Page
from app.google_health.data_types import REGISTRY
from app.google_health.pagination import PaginationPaused, RepeatedPageTokenError, iter_pages


class FakeClient:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.tokens = []
        self.kept = []

    async def list_data_points(self, spec, start, end, token):
        self.tokens.append(token)
        item = next(self.pages)
        if isinstance(item, Exception):
            raise item
        return item

    async def keep_after(self, responses, failure):
        self.kept.append((responses, failure))


@pytest.mark.asyncio
async def test_pagination_follows_every_token():
    client = FakeClient([Page([{"a": 1}], "next"), Page([{"a": 2}], None)])
    now = datetime.now(UTC)
    pages = [page async for page in iter_pages(client, REGISTRY["steps"], now, now)]
    assert len(pages) == 2
    assert client.tokens == [None, "next"]


@pytest.mark.parametrize(
    "initial,tokens",
    [(None, [" A ", "A", " A "]), (None, ["A", "B", "A"]), ("A", ["A"]), ("A", ["B", "A"])],
)
async def test_pagination_yields_cyclic_page_then_raises(monkeypatch, initial, tokens):
    # The budget ends exactly at the cyclic page: a cycle is still a failure, not a pause.
    monkeypatch.setattr(pagination, "MAX_PAGES_PER_CALL", len(tokens))
    client = FakeClient([Page([{"index": i}], t) for i, t in enumerate(tokens)])
    now = datetime.now(UTC)
    accepted = []
    with pytest.raises(RepeatedPageTokenError) as caught:
        async for page in iter_pages(client, REGISTRY["steps"], now, now, initial):
            accepted.append(page)
    assert [p.next_page_token for p in accepted] == tokens
    assert [p.data_points for p in accepted] == [[{"index": i}] for i in range(len(tokens))]
    assert [p.cyclic for p in accepted] == [False] * (len(tokens) - 1) + [True]
    # One request per page; the repeated continuation is never requested.
    assert client.tokens == [initial, *tokens[:-1]]
    # The consumer stores the cyclic page in its own transaction: nothing archived here.
    assert client.kept == []
    assert caught.value.__cause__ is caught.value.__context__ is None


async def test_pagination_does_not_raise_when_consumer_stops_at_cyclic_page():
    client = FakeClient([Page([{"index": 0}], "A"), Page([{"index": 1}], "A")])
    now = datetime.now(UTC)
    pages = iter_pages(client, REGISTRY["steps"], now, now)
    assert not (await anext(pages)).cyclic
    assert (await anext(pages)).cyclic
    await pages.aclose()
    assert client.tokens == [None, "A"] and client.kept == []


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


@pytest.mark.parametrize("limit", [1, 3])
@pytest.mark.parametrize("terminal", [False, True])
async def test_budget_bounds_requests_and_seen_tokens(monkeypatch, limit, terminal):
    monkeypatch.setattr(pagination, "MAX_PAGES_PER_CALL", limit)
    client = FakeClient(
        Page([], None if terminal and i == limit else f"page-{i}") for i in range(1, limit + 2)
    )
    now = datetime.now(UTC)
    pages = iter_pages(client, REGISTRY["steps"], now, now)
    for _ in range(limit):
        await anext(pages)
        seen = pages.ag_frame.f_locals["seen_tokens"]
        assert len(seen) <= limit
    with pytest.raises(StopAsyncIteration if terminal else PaginationPaused):
        await anext(pages)
    assert len(client.tokens) == limit and len(seen) <= limit


@pytest.mark.parametrize("reset_token", ["fresh", "expired"])
async def test_token_reset_does_not_refill_budget(monkeypatch, reset_token):
    monkeypatch.setattr(pagination, "MAX_PAGES_PER_CALL", 2)
    client = FakeClient([Page([], "expired"), InvalidPageTokenError(), Page([], reset_token)])
    resets = []

    async def reset():
        resets.append(True)

    now = datetime.now(UTC)
    with pytest.raises(PaginationPaused):
        _ = [p async for p in iter_pages(client, REGISTRY["steps"], now, now, on_reset=reset)]
    assert client.tokens == [None, "expired", None] and resets == [True]
