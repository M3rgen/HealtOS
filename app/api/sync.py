from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_job_request
from app.api.deps import runtime as runtime_dep
from app.api.deps import settings as settings_dep
from app.core.config import Settings
from app.db.session import get_session
from app.google_health.data_types import supported_data_types
from app.models.state import SyncState
from app.services.reconciliation_reports import list_runs
from app.services.retrieval_coverage import MAX_DAYS, CoverageLimit, coverage_report
from app.services.runtime import Runtime

router = APIRouter(prefix="/sync", tags=["sync"])
coverage_router = APIRouter(tags=["sync"])


@coverage_router.get("/api/sync/reconciliation")
async def reconciliation_history(
    offset: int = Query(0, ge=0, le=10000),
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await list_runs(session, offset, limit)


@coverage_router.get("/api/sync/coverage")
async def coverage(
    start_date: date,
    end_date: date,
    timezone: str = Query(..., min_length=1, max_length=100),
    data_type: str | None = Query(None, max_length=100),
    offset: int = Query(0, ge=0, le=1000),
    limit: int = Query(4, ge=1, le=4),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(settings_dep),
) -> dict:
    specs = supported_data_types(settings.extra_data_types)
    if (
        not 1 <= (end_date - start_date).days + 1 <= MAX_DAYS
        or start_date < date.min + timedelta(days=3)
        or end_date > date.max - timedelta(days=3)
    ):
        raise HTTPException(422, {"code": "coverage_invalid_range", "max_days": MAX_DAYS})
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(422, {"code": "coverage_invalid_timezone"}) from None
    if data_type is not None and data_type not in specs:
        raise HTTPException(422, {"code": "coverage_unknown_data_type", "accepted": sorted(specs)})
    try:
        return await coverage_report(
            session, specs, start_date, end_date, timezone, data_type, offset, limit
        )
    except CoverageLimit as exc:
        raise HTTPException(
            413, {"code": str(exc), "hint": "Tarih aralığını veya limit değerini azalt."}
        ) from None


def _serialize(item: SyncState, runtime: Runtime) -> dict:
    return {
        "data_type": item.data_type,
        "status": item.status,
        "last_requested_from": item.last_requested_from,
        "last_requested_to": item.last_requested_to,
        "last_successful_sync": item.last_successful_sync,
        "records_downloaded": item.records_downloaded,
        "last_error": item.last_error,
        "last_run": runtime.incremental.last_runs.get(item.data_type),
    }


@router.get("/status")
async def all_status(
    session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(runtime_dep)
) -> list[dict]:
    rows = (await session.scalars(select(SyncState).order_by(SyncState.data_type))).all()
    return [_serialize(row, runtime) for row in rows]


@router.get("/status/{data_type}")
async def one_status(
    data_type: str,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(runtime_dep),
) -> dict:
    row = await session.get(SyncState, data_type)
    if row is None:
        raise HTTPException(404, "No synchronization state for this data type")
    return _serialize(row, runtime)


@router.post(
    "/all", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_job_request)]
)
async def start_all(runtime: Runtime = Depends(runtime_dep)) -> dict:
    # Runs that finish with a higher sequence than this one belong to (or follow) this request.
    sequence = runtime.incremental.sequence
    runtime.spawn(runtime.incremental.run_all())
    return {
        "status": "accepted",
        "data_types": list(runtime.incremental.specs),
        "sequence": sequence,
    }


@router.post(
    "/{data_type}",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_job_request)],
)
async def start_one(data_type: str, runtime: Runtime = Depends(runtime_dep)) -> dict:
    if data_type not in runtime.incremental.specs:
        raise HTTPException(404, "Unsupported data type")
    runtime.spawn(runtime.incremental.run(data_type))
    return {"status": "accepted", "data_type": data_type}
