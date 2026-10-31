"""Short, stable error summaries for status endpoints and checkpoint rows.

`str(exc)` can carry upstream response text into the UI and the database. Status rows
keep only the exception class name plus a short code when one is known, for example
``GoogleHealthError (http_403)``. Rejected response contents stay in the raw archive.

Response validation failures (``InvalidResponseError``) take precedence over
message-based HTTP codes: they classify as ``invalid_response``, are never transient
and keep the saved pagination token, whatever code or text the body carried.
"""

from __future__ import annotations

import re

import httpx

from app.google_health.client import (
    GoogleHealthError,
    InvalidPageTokenError,
    InvalidResponseError,
    ResponseBodyTooLargeError,
)
from app.google_health.oauth import OAuthError

_HTTP_STATUS = re.compile(r"\bHTTP (\d{3})\b")
_CODES = (
    ("decoded byte budget", "response_too_large"),
    ("unsupported content encoding", "unsupported_encoding"),
    ("trailing bytes after its encoded content", "invalid_encoding"),
    ("connection failed", "connection"),
    ("invalid JSON", "invalid_json"),
    ("non-object JSON", "invalid_json"),
    ("invalid data point list", "invalid_response"),
    ("repeated pagination token", "repeated_page_token"),
)


def error_summary(exc: BaseException) -> str:
    code = error_code(exc)
    name = type(exc).__name__
    return f"{name} ({code})" if code else name


def error_code(exc: BaseException) -> str | None:
    if isinstance(exc, ResponseBodyTooLargeError):
        return "wire_response_too_large"
    if isinstance(exc, InvalidResponseError):
        return "invalid_response"
    if isinstance(exc, InvalidPageTokenError):
        return "invalid_page_token"
    message = str(exc)
    if match := _HTTP_STATUS.search(message):
        return f"http_{match.group(1)}"
    for fragment, code in _CODES:
        if fragment in message:
            return code
    return None


def is_transient(exc: BaseException) -> bool:
    """Retry only known network, rate-limit and server failures."""
    if isinstance(exc, OAuthError):
        return exc.transient
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, GoogleHealthError):
        code = error_code(exc)
        return code in {"connection", "http_429"} or bool(code and code.startswith("http_5"))
    return False
