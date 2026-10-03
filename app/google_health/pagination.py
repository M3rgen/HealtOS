from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

from app.google_health.client import GoogleHealthClient, InvalidPageTokenError, Page
from app.google_health.data_types import DataTypeSpec


async def iter_pages(
    client: GoogleHealthClient,
    spec: DataTypeSpec,
    start: datetime,
    end: datetime,
    page_token: str | None = None,
) -> AsyncIterator[Page]:
    seen_tokens: set[str] = set()
    token = page_token
    token_reset = False
    while True:
        try:
            page = await client.list_data_points(spec, start, end, token)
        except InvalidPageTokenError:
            if token_reset:
                raise
            token = None
            seen_tokens.clear()
            token_reset = True
            continue
        yield page
        if not page.next_page_token:
            return
        if page.next_page_token in seen_tokens:
            raise RuntimeError("Google Health API returned a repeated pagination token")
        seen_tokens.add(page.next_page_token)
        token = page.next_page_token
