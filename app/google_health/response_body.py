"""Fixed time and wire-byte budgets for receiving one response."""

from __future__ import annotations

import asyncio

import httpx

from app.google_health.response_close import close_response

# A body must deliver RESPONSE_PROGRESS_BYTES within every RESPONSE_BODY_SECONDS
# window, and finish within RESPONSE_BODY_MAX_SECONDS. A large page that streams
# slowly but steadily is accepted; a trickle or stall still ends. Each retry gets its
# own budget; close has a separate budget.
RESPONSE_BODY_SECONDS = 60.0
RESPONSE_PROGRESS_BYTES = 64 * 1024
RESPONSE_BODY_MAX_SECONDS = 600.0
# Count actual delivered bytes, regardless of Content-Length or Content-Encoding.
# Preserve the entire chunk crossing the limit, then stop requesting more. The
# retained wire size can exceed this ceiling by up to the last chunk's size.
# Joining/capture adds memory overhead: this is not a process-memory guarantee.
MAX_WIRE_BYTES = 64 * 1024 * 1024


class ResponseBodyLimitExceeded(ValueError):
    """A deterministic receive rejection; all delivered bytes remain available."""

    def __init__(self) -> None:
        super().__init__("Google Health response exceeded its wire byte budget")


class ResponseBodyTimeout(httpx.TimeoutException):
    """A retryable read failure whose text contains no upstream data."""

    def __init__(self) -> None:
        super().__init__("Google Health response body exceeded its time budget")


async def read_wire(response: httpx.Response, body: bytearray) -> None:
    """Extend the caller's buffer before reporting rejection or cancellation.

    The caller owns capture and close, including on failure. Keeping its buffer
    outside the timeout context preserves partial compressed bytes without decoding.
    A bytearray grows amortized linearly with delivered bytes: tiny chunks do not
    retain separate objects or list slots, and empty chunks consume no storage.
    Never concatenate immutable whole-body bytes here, which would repeatedly
    copy the prefix and make heavily fragmented responses quadratic to receive.
    No reader task is detached: timeout settles the read before close starts.
    """
    loop = asyncio.get_running_loop()
    final = loop.time() + RESPONSE_BODY_MAX_SECONDS
    deadline = min(loop.time() + RESPONSE_BODY_SECONDS, final)
    progress = 0
    budget = asyncio.timeout_at(deadline)
    try:
        async with budget:
            # aiter_raw() closes at EOF, conflating read and close cancellation.
            assert isinstance(response.stream, httpx.AsyncByteStream)
            async for chunk in response.stream:
                if chunk:
                    # Preserve the whole delivered chunk, even when it crosses
                    # the limit. No slice, truncation or next read is permitted.
                    body.extend(chunk)
                if len(body) > MAX_WIRE_BYTES:
                    raise ResponseBodyLimitExceeded()
                # Buffered streams may yield without suspending. Check the same
                # monotonic deadline so continuous chunks cannot starve the timer.
                # This check also runs for empty chunks; skipping their storage
                # must not let a continuously ready empty stream run forever.
                now = loop.time()
                if now >= deadline:
                    raise ResponseBodyTimeout()
                if len(body) - progress >= RESPONSE_PROGRESS_BYTES:
                    progress = len(body)
                    deadline = min(now + RESPONSE_BODY_SECONDS, final)
                    budget.reschedule(deadline)
    except TimeoutError as exc:
        if not budget.expired():
            raise
        # asyncio's context converts only its own cancellation. An external stop
        # remains CancelledError and cannot enter the client's retry path.
        raise ResponseBodyTimeout() from exc


async def read_body(
    response: httpx.Response, closing: list[asyncio.Future]
) -> tuple[bytes, BaseException | None, BaseException | None]:
    """The body bytes as received on the wire, before any Content-Encoding is undone.

    Return read and close failures separately: cancellation during close must not
    mark a body that reached EOF as incomplete. The caller captures before re-raising.

    Custom streams can raise ordinary exceptions as well as HTTPX transport errors.
    Both must return through capture, including when closing also fails. Keep the
    first failure unless cancellation intervenes; cleanup cannot turn a rejected
    read into a successful response or conceal the reason it was cut short.
    This function owns no retry policy: returning an exception does not classify
    it as a transport failure. The client decides only after retaining the wire.
    """
    # One mutable buffer across the read, cancellation and close paths. Storage
    # depends on wire size, rather than how the transport fragments those bytes.
    body = bytearray()
    cut_off: BaseException | None = None
    failure: BaseException | None = None
    try:
        try:
            await read_wire(response, body)
        except (Exception, asyncio.CancelledError) as exc:
            # Extension happened in read_wire before the next chunk was requested.
            # Even a non-HTTPX failure must leave those bytes available to capture.
            cut_off = failure = exc
    finally:
        # HTTPX marks a response closed before awaiting the stream's close. Keep one
        # shielded close task; restarting aclose() after cancellation would do nothing.
        close = asyncio.ensure_future(close_response(response))
        closing.append(close)
        if not isinstance(failure, asyncio.CancelledError):
            try:
                await asyncio.shield(close)
            except (Exception, asyncio.CancelledError) as exc:
                # Cancellation wins over a read error, so it cannot start a retry.
                # A close failure after EOF leaves cut_off unset: the bytes are
                # complete even though the response cannot authorize a Page.
                if failure is None or isinstance(exc, asyncio.CancelledError):
                    failure = exc
    # Freeze once, after cleanup, for capture/hash/decode. No immutable whole-body
    # copy is created per chunk; the temporary extra storage is at most one body.
    # The byte budget counts delivered data, not bytearray's spare capacity or
    # this final copy. Capture still receives immutable bytes on every exit path.
    return bytes(body), cut_off, failure
