"""One bounded close per response, shared by normal and cancelled requests."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import httpx

# Independent of HTTPX's socket timeouts: a stream's aclose() can wait without I/O.
# Tests shorten this budget without changing the retry count or request policy.
RESPONSE_CLOSE_SECONDS = 10.0


class ResponseCloseTimeout(httpx.TimeoutException):
    """A transient transport failure with no response or credential text."""

    def __init__(self) -> None:
        super().__init__("Google Health response close exceeded its time budget")


async def close_response(response: httpx.Response) -> None:
    """Close in the owning task, so timeout cancels and settles the same operation.

    There is no detached inner aclose task. The caller shields this task from stop
    signals and keeps it in the request's cleanup list. Timeout is a transport error,
    using the existing retry limit; cancellation by request cleanup stays cancellation.
    """
    try:
        async with asyncio.timeout(RESPONSE_CLOSE_SECONDS):
            await response.aclose()
    except TimeoutError as exc:
        raise ResponseCloseTimeout() from exc


async def cancel_closes(closing: Sequence[asyncio.Future]) -> None:
    """Cancel and join closes that outlasted the request's cancellation budget.

    Repeated stop signals cannot abandon a close between cancel() and its finally
    blocks. Also retrieve failures from already finished closes: no task exception
    should be left unobserved when the original cancellation propagates.
    This never starts a second aclose(), which HTTPX would treat as already closed.
    """
    if not closing:
        return
    for close in closing:
        if not close.done():
            close.cancel()
    settled = asyncio.gather(*closing, return_exceptions=True)
    while not settled.done():
        try:
            await asyncio.shield(settled)
        except asyncio.CancelledError:
            # The caller already has the cancellation to re-raise. Joining these
            # tasks must finish before it releases sync/backfill ownership.
            continue
    settled.result()
