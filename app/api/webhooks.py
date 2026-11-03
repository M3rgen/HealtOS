from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import bounded_body, require_webhook_budget
from app.api.deps import runtime as runtime_dep
from app.api.deps import settings as settings_dep
from app.core.config import Settings
from app.core.logging import event
from app.google_health.webhook import WebhookVerificationError
from app.services.runtime import Runtime
from app.services.webhook_metadata import UNPARSED_BODY, parse_webhook_body

logger = logging.getLogger(__name__)
router = APIRouter(tags=["webhooks"])


@router.get("/api/webhooks/status")
async def webhook_status(
    request: Request, response: Response, runtime: Runtime = Depends(runtime_dep)
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    try:
        status = await runtime.webhook_status.snapshot()
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Webhook status unavailable") from exc
    # Rate-limit refusals never reach the database; this process counts them in memory.
    limits = request.app.state.write_limits
    status["process_local"]["rate_limited"] = {
        "authorized": limits.webhook.rejected,
        "unauthorized": limits.webhook_unauthorized.rejected,
    }
    return status


@router.post("/webhooks/google-health")
async def google_health_webhook(
    request: Request,
    authorization: str | None = Header(None),
    signature: str | None = Header(None, alias="GOOGLE-HEALTH-API-SIGNATURE"),
    settings: Settings = Depends(settings_dep),
    runtime: Runtime = Depends(runtime_dep),
) -> Response:
    expected = settings.google_health_webhook_authorization
    configured = bool(expected) and expected != "CHANGE_ME"
    authorized = (
        configured
        and authorization is not None
        and hmac.compare_digest(authorization.encode("utf-8"), expected.encode("utf-8"))
    )
    require_webhook_budget(request, authorized=authorized)
    if not configured:
        await runtime.webhook_status.record("receiver_unconfigured")
        raise HTTPException(503, "Google Health webhook authorization is not configured")
    if not authorized:
        await runtime.webhook_status.record("authorization_rejected")
        event(logger, "WEBHOOK_SIGNATURE_INVALID", reason="authorization")
        raise HTTPException(401, "Invalid webhook authorization")
    try:
        raw = await bounded_body(
            request, settings.google_health_webhook_max_body_bytes, "Webhook payload is too large"
        )
    except HTTPException:
        await runtime.webhook_status.record("body_rejected")
        raise
    payload = parse_webhook_body(raw)
    # An unparsed body continues through signature verification: only authenticated
    # failures may enter the body-only archive and count as invalid_json (400 below).
    if payload == {"type": "verification"}:
        await runtime.webhook_status.record("challenge")
        return Response(status_code=201)
    if not signature:
        await runtime.webhook_status.record("missing_signature")
        event(logger, "WEBHOOK_SIGNATURE_INVALID", reason="missing_signature")
        raise HTTPException(401, "Missing webhook signature")
    try:
        await runtime.webhook_verifier.verify(raw, signature)
    except WebhookVerificationError as exc:
        await runtime.webhook_status.record(exc.reason)
        if exc.reason == "key_download_failed":
            # Not a verdict on the signature: a 5xx lets Google retry the delivery later.
            event(logger, "WEBHOOK_VERIFICATION_UNAVAILABLE", reason="key_download_failed")
            raise HTTPException(503, "Webhook verification temporarily unavailable") from exc
        event(logger, "WEBHOOK_SIGNATURE_INVALID", reason="cryptographic_verification")
        raise HTTPException(401, str(exc)) from exc
    await runtime.webhook_status.record("signature_valid")
    try:
        result = await runtime.webhook_processor.ingest_verified(raw, payload)
    except Exception:
        await runtime.webhook_status.record("ingestion_failed")
        raise
    event(
        logger,
        "WEBHOOK_BATCH_RECEIVED",
        batch_hash=result.batch_hash,
        element_count=result.element_count,
        inserted=result.inserted,
    )
    if result.should_process:
        runtime.spawn(runtime.webhook_processor.process_batch(result.batch_hash))
    if payload is UNPARSED_BODY:
        await runtime.webhook_status.record("invalid_json")
        raise HTTPException(400, "Invalid JSON payload")
    return Response(status_code=204)
