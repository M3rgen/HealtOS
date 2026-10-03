from __future__ import annotations

import hmac
import json
import logging
import re

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from app.api.deps import runtime as runtime_dep
from app.api.deps import settings as settings_dep
from app.core.config import Settings
from app.core.logging import event
from app.google_health.webhook import WebhookVerificationError
from app.services.runtime import Runtime

logger = logging.getLogger(__name__)
router = APIRouter(tags=["webhooks"])


async def bounded_body(request: Request, limit: int) -> bytes:
    lengths = request.headers.getlist("content-length")
    if lengths:
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,20}", lengths[0]):
            raise HTTPException(400, "Invalid Content-Length")
        if int(lengths[0]) > limit:
            raise HTTPException(413, "Webhook payload is too large")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, "Webhook payload is too large")
        body.extend(chunk)
    return bytes(body)


@router.post("/webhooks/google-health")
async def google_health_webhook(
    request: Request,
    authorization: str | None = Header(None),
    signature: str | None = Header(None, alias="GOOGLE-HEALTH-API-SIGNATURE"),
    settings: Settings = Depends(settings_dep),
    runtime: Runtime = Depends(runtime_dep),
) -> Response:
    if not settings.google_health_webhook_authorization or (
        settings.google_health_webhook_authorization == "CHANGE_ME"
    ):
        raise HTTPException(503, "Google Health webhook authorization is not configured")
    if not authorization or not hmac.compare_digest(
        authorization.encode("utf-8"), settings.google_health_webhook_authorization.encode("utf-8")
    ):
        event(logger, "WEBHOOK_SIGNATURE_INVALID", reason="authorization")
        raise HTTPException(401, "Invalid webhook authorization")
    raw = await bounded_body(request, settings.google_health_webhook_max_body_bytes)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "Invalid JSON payload") from exc
    if payload == {"type": "verification"}:
        return Response(status_code=201)
    if not signature:
        event(logger, "WEBHOOK_SIGNATURE_INVALID", reason="missing_signature")
        raise HTTPException(401, "Missing webhook signature")
    try:
        await runtime.webhook_verifier.verify(raw, signature)
    except WebhookVerificationError as exc:
        event(logger, "WEBHOOK_SIGNATURE_INVALID", reason="cryptographic_verification")
        raise HTTPException(401, str(exc)) from exc
    result = await runtime.webhook_processor.ingest_verified(raw, payload)
    event(
        logger,
        "WEBHOOK_BATCH_RECEIVED",
        batch_hash=result.batch_hash,
        element_count=result.element_count,
        inserted=result.inserted,
    )
    if result.should_process:
        runtime.spawn(runtime.webhook_processor.process_batch(result.batch_hash))
    return Response(status_code=204)
