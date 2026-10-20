"""Short, stable error summaries for status endpoints and checkpoint rows.

`str(exc)` can carry upstream response text into the UI and the database. Status rows
keep only the exception class name plus a short code when one is known, for example
``GoogleHealthError (http_403)``. Full details stay in the logs.
"""

from __future__ import annotations

import re

from app.google_health.client import InvalidPageTokenError

_HTTP_STATUS = re.compile(r"\bHTTP (\d{3})\b")
_CODES = (
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
    if isinstance(exc, InvalidPageTokenError):
        return "invalid_page_token"
    message = str(exc)
    if match := _HTTP_STATUS.search(message):
        return f"http_{match.group(1)}"
    for fragment, code in _CODES:
        if fragment in message:
            return code
    return None
