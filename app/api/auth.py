from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse

from app.api.deps import runtime as runtime_dep
from app.api.deps import settings as settings_dep
from app.core.config import Settings
from app.google_health.data_types import readonly_scopes
from app.services.runtime import Runtime

router = APIRouter(prefix="/auth/google", tags=["auth"])


def _state(settings: Settings) -> str:
    secret = settings.google_token_encryption_key
    if not secret:
        raise HTTPException(503, "GOOGLE_TOKEN_ENCRYPTION_KEY must be configured before OAuth")
    payload = f"{int(time.time())}.{secrets.token_urlsafe(18)}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def _verify_state(settings: Settings, state: str) -> None:
    try:
        timestamp, nonce, signature = state.split(".", 2)
        payload = f"{timestamp}.{nonce}"
        expected = hmac.new(
            settings.google_token_encryption_key.encode(), payload.encode(), hashlib.sha256
        ).digest()
        actual = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        fresh = abs(time.time() - int(timestamp)) < 600
    except (AttributeError, ValueError):
        fresh = False
        expected = actual = b""
    if not fresh or not hmac.compare_digest(expected, actual):
        raise HTTPException(400, "Invalid or expired OAuth state")


@router.get("/start")
async def start(
    settings: Settings = Depends(settings_dep), runtime: Runtime = Depends(runtime_dep)
):
    if not settings.credentials_configured:
        raise HTTPException(503, "Google OAuth client credentials are not configured")
    url = runtime.oauth.authorization_url(
        readonly_scopes(settings.extra_data_types, settings.enabled_data_types),
        _state(settings),
    )
    return RedirectResponse(url)


@router.get("/callback")
async def callback(
    code: str = Query(...),
    state: str = Query(...),
    settings: Settings = Depends(settings_dep),
    runtime: Runtime = Depends(runtime_dep),
) -> dict:
    _verify_state(settings, state)
    token = await runtime.oauth.exchange_code(code)
    # Verify the candidate account without using or mutating the active token cache.
    identity = await runtime.google.get_identity(access_token=token["access_token"])
    health_user_id = identity.get("healthUserId")
    if not isinstance(health_user_id, str) or not health_user_id:
        raise HTTPException(502, "Google Health did not return a valid account identity")
    async with runtime.tokens.credential_update():
        refresh_token = token.get("refresh_token")
        scopes = token.get("scope")
        if not refresh_token:
            refresh_token = await runtime.credentials.load_refresh_token()
            if not refresh_token:
                raise HTTPException(
                    400, "Google did not return an offline refresh token; revoke and retry consent"
                )
            # An existing offline token is reusable only for this verified account.
            existing = await runtime.oauth.refresh(refresh_token)
            existing_identity = await runtime.google.get_identity(access_token=existing.value)
            if existing_identity.get("healthUserId") != health_user_id:
                raise HTTPException(
                    400, "Google did not return an offline token for this account; retry consent"
                )
            refresh_token = existing.replacement_refresh_token or refresh_token
            scopes = existing.scopes or scopes
        await runtime.credentials.save_connection(
            refresh_token, scopes, health_user_id, identity.get("legacyUserId")
        )
    return {"status": "connected", "health_user_id": health_user_id}
