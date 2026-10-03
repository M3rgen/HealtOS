from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_job_request
from app.api.deps import runtime as runtime_dep
from app.db.session import get_session
from app.models.state import SyncState
from app.services.runtime import Runtime

router = APIRouter(prefix="/sync", tags=["sync"])


def _serialize(item: SyncState) -> dict:
    return {
        "data_type": item.data_type,
        "status": item.status,
        "last_requested_from": item.last_requested_from,
        "last_requested_to": item.last_requested_to,
        "last_successful_sync": item.last_successful_sync,
        "records_downloaded": item.records_downloaded,
        "last_error": item.last_error,
    }


@router.get("/status")
async def all_status(session: AsyncSession = Depends(get_session)) -> list[dict]:
    rows = (await session.scalars(select(SyncState).order_by(SyncState.data_type))).all()
    return [_serialize(row) for row in rows]


@router.get("/status/{data_type}")
async def one_status(data_type: str, session: AsyncSession = Depends(get_session)) -> dict:
    row = await session.get(SyncState, data_type)
    if row is None:
        raise HTTPException(404, "No synchronization state for this data type")
    return _serialize(row)


@router.post(
    "/all", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_job_request)]
)
async def start_all(runtime: Runtime = Depends(runtime_dep)) -> dict:
    runtime.spawn(runtime.incremental.run_all())
    return {"status": "accepted", "data_types": list(runtime.incremental.specs)}


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
