from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime

from app.google_health.client import GoogleHealthClient, InvalidPageTokenError, Page
from app.google_health.data_types import DataTypeSpec

# A turn must release ownership even if Google always returns a new continuation.
MAX_PAGES_PER_CALL = 1000


class PaginationPaused(Exception):
    """The committed continuation must be resumed by a later invocation, not retried."""


class PageBudget:
    """Shared by all windows one backfill or sync invocation fetches for a data type."""

    def __init__(self):
        self.remaining = MAX_PAGES_PER_CALL


async def iter_pages(
    client: GoogleHealthClient,
    spec: DataTypeSpec,
    start: datetime,
    end: datetime,
    page_token: str | None = None,
    *,
    on_reset: Callable[[], Awaitable[None]] | None = None,
    budget: PageBudget | None = None,
) -> AsyncIterator[Page]:
    budget = budget if budget is not None else PageBudget()
    seen_tokens: set[str] = set()
    token = page_token
    token_reset = False
    while True:
        if budget.remaining <= 0:
            raise PaginationPaused("Google Health page budget exhausted")
        try:
            page = await client.list_data_points(spec, start, end, token)
        except InvalidPageTokenError:
            if token_reset:
                raise
            if on_reset is not None:
                await on_reset()
            token = None
            seen_tokens.clear()
            token_reset = True
            continue
        budget.remaining -= 1
        yield page
        if not page.next_page_token:
            return
        if page.next_page_token in seen_tokens:
            raise RuntimeError("Google Health API returned a repeated pagination token")
        seen_tokens.add(page.next_page_token)
        token = page.next_page_token
