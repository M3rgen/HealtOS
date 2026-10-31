from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.core.config import Settings
from app.core.logging import event
from app.google_health import strict_json
from app.google_health.oauth_response import (
    TokenResponseRejected,
    finish_response,
    read_token_body,
)

logger = logging.getLogger(__name__)

TOKEN_REQUEST_ATTEMPTS = 4
TOKEN_REQUEST_TIMEOUT_SECONDS = 30


@dataclass
class AccessToken:
    value: str
    expires_at: float
    replacement_refresh_token: str | None = None
    scopes: str | None = None

    def valid(self) -> bool:
        return self.expires_at > time.time() + 60


class OAuthError(RuntimeError):
    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


_JSON_ERRORS = {
    strict_json.INVALID: "OAuth token endpoint returned invalid JSON",
    strict_json.NOT_OBJECT: "OAuth response did not contain an access token",
    strict_json.DUPLICATE_KEY: "OAuth token endpoint returned duplicate JSON object keys",
    strict_json.CONSTANT: "OAuth token endpoint returned non-standard JSON constants",
    # Presence alone rejects the envelope; its code and message never steer retries.
    strict_json.ERROR_FIELD: "OAuth token endpoint returned an error response",
}


# RFC 9110 field-value after "Bearer ": visible ASCII, inner SP/HTAB, no trailing
# whitespace. Everything h11 rejects (NUL, CR, LF, VT, FF, trailing SP/HTAB) is
# rejected; other controls and DEL are invalid too. isascii keeps unpaired
# surrogates from raising an encoding error that would contain the token.
_FIELD_CHARS = frozenset(map(chr, range(0x21, 0x7F))) | {" ", "\t"}


def _header_safe(token: str) -> bool:
    return token.isascii() and set(token) <= _FIELD_CHARS and token[-1] not in " \t"


def _token_payload(body: bytes) -> dict:
    try:
        return strict_json.loads_object(body)
    except strict_json.StrictJsonError as exc:
        message = _JSON_ERRORS[exc.kind]
    # Token bodies hold credentials: fixed text, no parser chain, never transient.
    raise OAuthError(message)


async def _check_token_status(response: httpx.Response) -> None:
    # HTTPX builds next_request even with redirects disabled, parsing Location.
    # Reject before that step so a malformed target cannot become a retried
    # transport error containing header text. Other users of this client pass through.
    if not response.request.extensions.get("healthos_oauth_token_request"):
        return
    if response.is_success and response.status_code != 206:
        return
    # Partial credentials cannot authorize either grant, even if their JSON is
    # otherwise valid. Reject before reading/decompressing/parsing the body;
    # Content-Range is irrelevant and must not enter errors, logs or the archive.
    # Keep other success codes and the existing 429/5xx retry policy unchanged.
    failure = await finish_response(
        response,
        OAuthError(
            f"OAuth token endpoint returned HTTP {response.status_code}",
            transient=response.status_code == 429 or 500 <= response.status_code < 600,
        ),
    )
    # HTTPX's exception handler calls aclose again; it is already closed, so
    # malformed Location/status rejection cannot enter an unbounded close.
    assert failure is not None
    raise failure


def _token_expiry(value: object) -> float:
    """A finite cache deadline from positive seconds; fractions are rounded down."""
    message = "OAuth response contained an invalid token lifetime"
    # bool is an int subclass, not a lifetime; containers/null are never coerced.
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise OAuthError(message)
    try:
        seconds = math.floor(float(value))  # NaN/inf raise here
        expires_at = time.time() + seconds
    except (ValueError, OverflowError):
        # The error text can contain the source value; omit the chain.
        raise OAuthError(message) from None
    if seconds <= 0 or not math.isfinite(expires_at):
        raise OAuthError(message)
    return expires_at


class OAuthClient:
    authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"

    def __init__(
        self, settings: Settings, http: httpx.AsyncClient | None = None, sleep=asyncio.sleep
    ):
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=30)
        self.sleep = sleep

    def authorization_url(self, scopes: list[str], state: str) -> str:
        params = {
            "client_id": self.settings.google_client_id,
            "redirect_uri": self.settings.google_redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "state": state,
        }
        return f"{self.authorize_url}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> dict:
        # An authorization code is single-use: a second attempt would only report
        # invalid_grant and hide the network failure that actually happened.
        return await self._token_request(
            {
                "code": code,
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret,
                "redirect_uri": self.settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
            attempts=1,
        )

    async def refresh(self, refresh_token: str) -> AccessToken:
        payload = await self._token_request(
            {
                "refresh_token": refresh_token,
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret,
                "grant_type": "refresh_token",
            }
        )
        expires_at = _token_expiry(payload.get("expires_in", 3600))
        replacement = payload.get("refresh_token")
        scopes = payload.get("scope")
        token = AccessToken(
            payload["access_token"],
            expires_at,
            replacement if isinstance(replacement, str) and replacement else None,
            scopes if isinstance(scopes, str) and scopes else None,
        )
        event(logger, "TOKEN_REFRESHED")
        return token

    async def _token_request(
        self, data: dict[str, str], attempts: int = TOKEN_REQUEST_ATTEMPTS
    ) -> dict:
        # Bound the whole operation, including slow requests and backoff, not just
        # each attempt. A missing network must not stall a recovery for minutes.
        try:
            async with asyncio.timeout(TOKEN_REQUEST_TIMEOUT_SECONDS) as budget:
                deadline = budget.when()
                assert deadline is not None
                for attempt in range(1, attempts + 1):
                    try:
                        return await self._token_attempt(data, deadline)
                    except OAuthError as exc:
                        if not exc.transient or attempt == attempts:
                            raise
                        delay = 2 ** (attempt - 1)
                        event(
                            logger,
                            "TOKEN_REFRESH_RETRY",
                            attempt=attempt + 1,
                            max_attempts=TOKEN_REQUEST_ATTEMPTS,
                            delay_seconds=delay,
                        )
                        await self.sleep(delay)
                raise AssertionError("Token request attempts exhausted without an outcome")
        except TimeoutError:
            # Do not retain read/close exceptions that could contain credentials.
            pass
        raise OAuthError(
            "OAuth token endpoint connection failed", transient=True
        ) from TimeoutError()

    async def _token_attempt(self, data: dict[str, str], deadline: float) -> dict:
        # Install before each attempt: injected clients may replace their hooks.
        # Reject unsafe statuses before other hooks can inspect the response.
        if _check_token_status not in self.http.event_hooks["response"]:
            self.http.event_hooks["response"].insert(0, _check_token_status)
        body = b""
        try:
            request = self.http.build_request(
                "POST",
                self.token_url,
                data=data,
                extensions={"healthos_oauth_token_request": True},
            )
            response = await self.http.send(request, stream=True, follow_redirects=False)
            body = await read_token_body(response, deadline)
        except TokenResponseRejected as exc:
            message, transient = str(exc), False
        except httpx.TransportError:
            message, transient = "OAuth token endpoint connection failed", True
        else:
            message, transient = "", False
        if message:
            # Raise outside the handler: no upstream exception chain may retain
            # token chunks, transport details or decoder inputs.
            raise OAuthError(message, transient=transient)
        payload = _token_payload(body)
        access_token = payload.get("access_token")
        # Check whitespace without changing the opaque token supplied by Google.
        if not isinstance(access_token, str) or not access_token.strip():
            raise OAuthError("OAuth response did not contain an access token")
        # Reject before rotation, identity lookup or caching: HTTPX/h11 would quote
        # an unusable Authorization value, token included, in a transport error.
        if not _header_safe(access_token):
            raise OAuthError("OAuth response contained an invalid access token")
        refresh_token = payload.get("refresh_token")
        # Missing/null fields keep the existing offline credential. All other JSON
        # types must be strings, including false/zero and empty containers; accepting
        # a candidate access token could hide an unusable refresh-token rotation.
        if refresh_token is not None and not isinstance(refresh_token, str):
            raise OAuthError(
                "OAuth response contained an invalid refresh token",
                transient=False,
            ) from None
        # A nonempty whitespace-only rotation would destroy the offline credential.
        # Validate both grants before identity lookup, storage, caching or success.
        # Preserve absent/empty fields and every byte of a usable opaque token.
        if isinstance(refresh_token, str) and refresh_token and not refresh_token.strip():
            raise OAuthError(
                "OAuth response contained a blank refresh token",
                transient=False,
            ) from None
        return payload


class TokenProvider:
    def __init__(self, oauth: OAuthClient, refresh_token_loader, refresh_token_saver=None):
        self.oauth = oauth
        self.refresh_token_loader = refresh_token_loader
        self.refresh_token_saver = refresh_token_saver
        self._token: AccessToken | None = None
        self._lock = asyncio.Lock()
        self._generation = 0

    def invalidate(self) -> None:
        self._generation += 1
        self._token = None

    @asynccontextmanager
    async def credential_update(self):
        """Serialize activation with refresh, invalidating only after a successful commit."""
        async with self._lock:
            yield
            self.invalidate()

    async def get(self) -> str:
        if self._token and self._token.valid():
            return self._token.value
        async with self._lock:
            if self._token and self._token.valid():
                return self._token.value
            while True:
                generation = self._generation
                refresh_token = await self.refresh_token_loader()
                if not refresh_token:
                    raise OAuthError("No Google OAuth refresh token is configured")
                candidate = await self.oauth.refresh(refresh_token)
                if generation != self._generation:
                    continue  # Relinking/invalidation made the in-flight response obsolete.
                if candidate.replacement_refresh_token and self.refresh_token_saver:
                    await self.refresh_token_saver(
                        candidate.replacement_refresh_token,
                        candidate.scopes,
                    )
                if generation != self._generation:
                    continue
                self._token = candidate
                return candidate.value
