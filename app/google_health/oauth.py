from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.core.config import Settings
from app.core.logging import event

logger = logging.getLogger(__name__)


@dataclass
class AccessToken:
    value: str
    expires_at: float
    replacement_refresh_token: str | None = None
    scopes: str | None = None

    def valid(self) -> bool:
        return self.expires_at > time.time() + 60


class OAuthError(RuntimeError):
    pass


class OAuthClient:
    authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=30)

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
        return await self._token_request(
            {
                "code": code,
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret,
                "redirect_uri": self.settings.google_redirect_uri,
                "grant_type": "authorization_code",
            }
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
        event(logger, "TOKEN_REFRESHED")
        try:
            expires_in = int(payload.get("expires_in", 3600))
        except (TypeError, ValueError) as exc:
            raise OAuthError("OAuth response contained an invalid token lifetime") from exc
        replacement = payload.get("refresh_token")
        scopes = payload.get("scope")
        return AccessToken(
            payload["access_token"],
            time.time() + expires_in,
            replacement if isinstance(replacement, str) and replacement else None,
            scopes if isinstance(scopes, str) and scopes else None,
        )

    async def _token_request(self, data: dict[str, str]) -> dict:
        try:
            response = await self.http.post(self.token_url, data=data)
        except httpx.TransportError as exc:
            raise OAuthError("OAuth token endpoint connection failed") from exc
        if response.is_error:
            raise OAuthError(f"OAuth token endpoint returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise OAuthError("OAuth token endpoint returned invalid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str):
            raise OAuthError("OAuth response did not contain an access token")
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
