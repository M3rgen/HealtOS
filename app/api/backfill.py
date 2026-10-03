from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_job_request
from app.api.deps import runtime as runtime_dep
from app.db.session import get_session
from app.models.state import BackfillJob
from app.services.runtime import Runtime

router = APIRouter(prefix="/backfill", tags=["backfill"])


@router.post(
    "/start", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_job_request)]
)
async def start(
    data_type: str | None = Query(None), runtime: Runtime = Depends(runtime_dep)
) -> dict:
    if data_type and data_type not in runtime.backfill.specs:
        raise HTTPException(404, "Unsupported data type")
    selected = [data_type] if data_type else None
    runtime.spawn(runtime.backfill.run(selected))
    return {"status": "accepted", "data_types": selected or list(runtime.backfill.specs)}


@router.get("/status")
async def status_endpoint(session: AsyncSession = Depends(get_session)) -> list[dict]:
    rows = (
        await session.execute(
            select(
                BackfillJob.data_type,
                BackfillJob.status,
                func.count(BackfillJob.id),
                func.sum(BackfillJob.records_downloaded),
            )
            .group_by(BackfillJob.data_type, BackfillJob.status)
            .order_by(BackfillJob.data_type, BackfillJob.status)
        )
    ).all()
    return [
        {"data_type": row[0], "status": row[1], "windows": row[2], "records": row[3] or 0}
        for row in rows
    ]
