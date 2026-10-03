from __future__ import annotations

import re
from urllib.parse import urlsplit

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

WEBHOOK_PATH = "/webhooks/google-health"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})
AUTHORITY = re.compile(r"(?P<host>\[[0-9a-fA-F:]+\]|[a-zA-Z0-9.-]+)(?::(?P<port>[0-9]{1,5}))?")


def hostname(authority: str) -> str | None:
    match = AUTHORITY.fullmatch(authority)
    if match is None:
        return None
    port = match.group("port")
    if port is not None and not 1 <= int(port) <= 65535:
        return None
    return match.group("host").lower()


class HostValidationMiddleware:
    """Keep private routes local; public webhook hosts get only the receiver."""

    def __init__(self, app: ASGIApp, webhook_endpoint_uri: str | None = None) -> None:
        self.app = app
        self.webhook_host = None
        if webhook_endpoint_uri:
            try:
                endpoint = urlsplit(webhook_endpoint_uri)
                if endpoint.scheme == "https" and endpoint.path == WEBHOOK_PATH:
                    self.webhook_host = hostname(endpoint.netloc)
            except ValueError:
                pass  # Invalid configuration must not expand the trusted host set.

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        hosts = [value for name, value in scope["headers"] if name.lower() == b"host"]
        host = hostname(hosts[0].decode("latin-1")) if len(hosts) == 1 else None
        local = host in LOCAL_HOSTS
        public_webhook = (
            host is not None
            and host == self.webhook_host
            and scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"] == WEBHOOK_PATH
        )
        if local or public_webhook:
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": "Invalid host"})
        else:
            await PlainTextResponse("Invalid host", status_code=400)(scope, receive, send)
