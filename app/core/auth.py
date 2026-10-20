"""Single-user bearer-token barrier in front of the local UI and API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections.abc import Iterable
from urllib.parse import urlencode, urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.host_validation import WEBHOOK_PATH, hostname

COOKIE_NAME = "healthos_ui"
COOKIE_MAX_AGE = 30 * 24 * 3600
LOGIN_PATH = "/login"
# Requests that must work without the UI token. The OAuth callback arrives as a
# cross-site redirect (SameSite=Strict cookies are withheld) and is guarded by the
# HMAC-signed state that only the authenticated /auth/google/start can mint.
EXEMPT = frozenset(
    {
        ("POST", WEBHOOK_PATH),
        ("GET", "/health/live"),
        ("HEAD", "/health/live"),
        ("GET", LOGIN_PATH),
        ("HEAD", LOGIN_PATH),
        ("POST", LOGIN_PATH),
        ("GET", "/auth/google/callback"),
    }
)


def cookie_value(token: str) -> str:
    """Session cookie derived from the token, so the raw token never sits in the jar."""
    digest = hmac.new(token.encode(), b"healthos-ui-cookie-v1", hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def token_matches(token: str, candidate: str | None) -> bool:
    return candidate is not None and secrets.compare_digest(candidate.encode(), token.encode())


def _cookies(header: str) -> Iterable[tuple[str, str]]:
    for item in header.split(";"):
        name, separator, value = item.strip().partition("=")
        if separator:
            yield name, value


def request_authenticated(token: str | None, headers: Headers) -> bool:
    if not token:
        return True
    authorization = headers.getlist("authorization")
    if len(authorization) == 1:
        scheme, _, credentials = authorization[0].partition(" ")
        if scheme.lower() == "bearer" and token_matches(token, credentials.strip()):
            return True
    expected = cookie_value(token)
    return any(
        name == COOKIE_NAME and token_matches(expected, value)
        for header in headers.getlist("cookie")
        for name, value in _cookies(header)
    )


def safe_next(value: str | None) -> str:
    """Return a same-site relative path, falling back to the dashboard."""
    if not value or len(value) > 2048 or not value.startswith("/"):
        return "/"
    if value.startswith(("//", "/\\")) or any(ord(c) < 0x21 or c in '\\"<>' for c in value):
        return "/"
    try:
        parts = urlsplit(value)
    except ValueError:
        return "/"
    if parts.scheme or parts.netloc or parts.path.startswith(LOGIN_PATH):
        return "/"
    return value


def same_origin_form(headers: Headers, scheme: str) -> bool:
    """CSRF check for the login form: same origin as the Host the request reached."""
    fetch_sites = {value.lower() for value in headers.getlist("sec-fetch-site")}
    if fetch_sites & {"cross-site", "same-site"}:
        return False  # same-site still includes other localhost ports.
    origins = headers.getlist("origin")
    if not origins:
        return True  # Non-browser clients; browsers always send Origin on form POSTs.
    hosts = headers.getlist("host")
    if len(origins) != 1 or len(hosts) != 1 or hostname(hosts[0]) is None:
        return False
    try:
        origin = urlsplit(origins[0])
        target = urlsplit(f"{scheme}://{hosts[0]}")
        default = 443 if scheme == "https" else 80
        return (
            origins[0] == f"{origin.scheme}://{origin.netloc}"
            and origin.scheme == target.scheme
            and origin.hostname == target.hostname
            and (origin.port or default) == (target.port or default)
            and origin.username is None
        )
    except ValueError:
        return False


def _wants_html(scope: Scope, headers: Headers) -> bool:
    if scope["method"] not in {"GET", "HEAD"} or scope["path"].startswith("/api/"):
        return False
    return headers.get("sec-fetch-mode") == "navigate" or "text/html" in headers.get("accept", "")


class UITokenMiddleware:
    """Require APP_UI_TOKEN (bearer header or login cookie) for every private route."""

    def __init__(self, app: ASGIApp, token: str | None) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.token or scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        exempt = scope["type"] == "http" and (scope["method"], scope["path"]) in EXEMPT
        if exempt or request_authenticated(self.token, headers):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": "auth_required"})
            return
        if _wants_html(scope, headers):
            target = scope["path"]
            if scope.get("query_string"):
                target += "?" + scope["query_string"].decode("latin-1")
            response = RedirectResponse(
                f"{LOGIN_PATH}?{urlencode({'next': safe_next(target)})}",
                status_code=303,
                headers={"Cache-Control": "no-store"},
            )
        else:
            response = JSONResponse(
                {"detail": "auth_required"},
                status_code=401,
                headers={
                    "WWW-Authenticate": 'Bearer realm="HealthOS"',
                    "Cache-Control": "no-store",
                },
            )
        await response(scope, receive, send)
