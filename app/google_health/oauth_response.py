"""Receive credentials without HTTPX's unbounded read/decompression path."""

from __future__ import annotations

import asyncio

import httpx

from app.google_health import response_decode
from app.google_health.response_close import cancel_closes, close_response

MAX_TOKEN_WIRE_BYTES = 1024 * 1024
MAX_TOKEN_DECODED_BYTES = 1024 * 1024


class TokenResponseRejected(ValueError):
    """A fixed, non-retryable rejection without any credential-bearing data."""


async def finish_response(
    response: httpx.Response, failure: BaseException | None = None
) -> BaseException | None:
    # One owned close task, with its own deadline. Settle it before propagating
    # cancellation (including repeated cancellation); never leave cleanup detached.
    close = asyncio.ensure_future(close_response(response))
    try:
        await asyncio.shield(close)
    except asyncio.CancelledError as exc:
        await cancel_closes([close])
        return exc
    except Exception as exc:
        if failure is None:
            return exc
    return failure


async def read_token_body(response: httpx.Response, deadline: float) -> bytes:
    body = bytearray()
    failure = None
    decoded = b""
    try:
        # Iterate the raw stream directly: aiter_bytes inflates without a cap,
        # aiter_raw closes at EOF before we can bound that close separately.
        assert isinstance(response.stream, httpx.AsyncByteStream)
        async for chunk in response.stream:
            if len(chunk) > MAX_TOKEN_WIRE_BYTES - len(body):
                # Do not retain the excess chunk or request the following one.
                raise TokenResponseRejected("OAuth token response exceeded its byte budget")
            body.extend(chunk)
            # Continuously ready streams must not starve the operation's timer.
            if asyncio.get_running_loop().time() >= deadline:
                raise httpx.TimeoutException("OAuth token endpoint connection failed")
        decoded, error = response_decode.decode(
            bytes(body), response.headers, None, max_decoded_bytes=MAX_TOKEN_DECODED_BYTES
        )
        if isinstance(error, response_decode.ResponseDecodeLimitExceeded):
            raise TokenResponseRejected("OAuth token response exceeded its byte budget")
        if error is not None:
            raise TokenResponseRejected("OAuth token response content decoding failed")
    except BaseException as exc:
        failure = exc
    failure = await finish_response(response, failure)
    if failure is not None:
        if isinstance(failure, Exception) and not isinstance(failure, TokenResponseRejected):
            # Custom streams can also raise non-HTTPX errors containing body text.
            raise httpx.TransportError("OAuth token endpoint connection failed")
        raise failure
    return decoded
