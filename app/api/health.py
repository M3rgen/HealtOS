from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.logging import event

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/health", response_model=None)
async def health(request: Request) -> dict | JSONResponse:
    runtime = request.app.state.runtime
    settings = request.app.state.settings
    scheduler_expected = settings.app_env != "test" and (
        settings.reconciliation_enabled or settings.watchdog_enabled
    )
    scheduler_status = (
        "running"
        if runtime.scheduler_running
        else "disabled"
        if not scheduler_expected
        else "stopped"
    )
    configuration_status = (
        "healthy"
        if settings.credentials_configured
        and settings.webhook_readiness["external_registration_ready"]
        else "degraded"
    )
    try:
        snapshot = await runtime.pipeline_health.inspect(
            scheduler_running=not scheduler_expected or runtime.scheduler_running
        )
    except Exception as exc:
        event(logger, "HEALTH_DATABASE_UNAVAILABLE", error_type=type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={
                "status": "failed",
                "database": "unavailable",
                "scheduler": scheduler_status,
                "configuration": configuration_status,
                "service": "personal-health-mirror",
            },
        )
    payload = snapshot.as_dict(
        database="healthy",
        scheduler=scheduler_status,
        configuration=configuration_status,
    )
    if configuration_status != "healthy" and payload["status"] == "healthy":
        payload["status"] = "degraded"
        payload["reasons"].append("configuration_incomplete")
    payload["service"] = "personal-health-mirror"
    payload["watchdog"] = "enabled" if settings.watchdog_enabled else "disabled"
    return payload
