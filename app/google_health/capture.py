"""Every HTTP response body received from the Google Health API, as received.

The client records one :class:`CapturedResponse` per HTTP exchange that produced a
response (success, error status, retry, or a body cut off by a timeout): the entity body
byte for byte, the request (method, path, query string, JSON body: never a header, so
never a token), the status and a few response headers. Pages travel with their records
and are stored in the same transaction (``app.services.storage.store_page``); everything
else (failures, identity, devices, probes) goes to a :class:`ResponseArchive`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from urllib.parse import parse_qsl, urlencode

# Response headers kept with a body: enough to read and date it, nothing that identifies
# the caller.
KEPT_HEADERS = ("content-type", "content-encoding", "content-length", "date", "retry-after")
# Query parameters that could carry a credential; never stored (Google Health takes the
# token as a header, so this is a safety net).
SECRET_PARAMETERS = frozenset({"access_token", "oauth_token", "key", "token", "client_secret"})


@dataclass(frozen=True)
class CapturedResponse:
    fetched_at: datetime
    source: str
    context_id: str | None
    data_type: str | None
    method: str
    path: str
    query: str | None
    request_body: bytes | None
    attempt: int
    status_code: int
    headers: dict[str, str]
    body: bytes
    # False when the connection failed before the body ended (`body` is what arrived).
    complete: bool = True
    error: str | None = None
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "sha256", hashlib.sha256(self.body).hexdigest())

    def row(self) -> dict:
        return {
            "fetched_at": self.fetched_at,
            "source": self.source,
            "context_id": self.context_id,
            "data_type": self.data_type,
            "method": self.method,
            "path": self.path,
            "query": self.query,
            "request_body": self.request_body,
            "attempt": self.attempt,
            "status_code": self.status_code,
            "headers": self.headers,
            "body": self.body,
            "body_sha256": self.sha256,
            "body_size": len(self.body),
            "complete": self.complete,
            "error": self.error,
        }


class ResponseArchive(Protocol):
    async def keep(self, responses: Sequence[CapturedResponse]) -> None: ...


def safe_query(raw: bytes) -> str | None:
    """The query string exactly as sent, minus any credential-like parameter."""
    if not raw:
        return None
    query = raw.decode("ascii")
    pairs = parse_qsl(query, keep_blank_values=True)
    if any(name.lower() in SECRET_PARAMETERS for name, _ in pairs):
        query = urlencode([(n, v) for n, v in pairs if n.lower() not in SECRET_PARAMETERS])
    return query or None


def kept_headers(headers) -> dict[str, str]:
    return {name: headers[name] for name in KEPT_HEADERS if name in headers}
