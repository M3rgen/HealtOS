from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import runtime as runtime_dep
from app.google_health.client import GoogleHealthError
from app.google_health.oauth import OAuthError
from app.services.runtime import Runtime

router = APIRouter(prefix="/google-health", tags=["google-health"])


@router.get("/capabilities")
async def capabilities(
    days: int = Query(30, ge=1, le=365), runtime: Runtime = Depends(runtime_dep)
) -> dict:
    try:
        return {
            "lookback_days": days,
            "data_types": await runtime.discovery.probe(days),
        }
    except (OAuthError, GoogleHealthError) as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/devices")
async def devices(runtime: Runtime = Depends(runtime_dep)) -> dict:
    try:
        return {"devices": await runtime.discovery.devices()}
    except (OAuthError, GoogleHealthError) as exc:
        raise HTTPException(503, str(exc)) from exc
