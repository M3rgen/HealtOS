"""Reconciliation lifecycle and transaction-bound difference counters.

An unfinished run stays running, never successful. Counters share the raw page or
deletion transaction, including its rollback. A scope follows the caller's requested
window only; incremental disables it while finishing an older foreign checkpoint.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

from sqlalchemy import func, select, update

from app.google_health.pagination import PaginationPaused
from app.models.reconciliation import ReconciliationResult as Result
from app.models.reconciliation import ReconciliationRun as Run
from app.services.storage import StoreStats

_scope: ContextVar[tuple[uuid.UUID, str] | None] = ContextVar("reconciliation_report", default=None)


@contextmanager
def reporting(run_id: uuid.UUID | None, data_type: str = "") -> Iterator[None]:
    token = _scope.set((run_id, data_type) if run_id else None)
    try:
        yield
    finally:
        _scope.reset(token)


async def record_changes(session, data_type: str, stats: StoreStats, *, page=False) -> None:
    """Must be called inside the transaction that made these changes."""
    scope = _scope.get()
    if scope is None or scope[1] != data_type:
        return
    result = await session.execute(
        update(Result)
        .where(Result.run_id == scope[0], Result.data_type == data_type, Result.status == "running")
        .values(
            inserted=Result.inserted + stats.inserted,
            updated=Result.updated + stats.updated,
            unchanged=Result.unchanged + stats.unchanged,
            source_deleted=Result.source_deleted + stats.source_deleted,
            pages_committed=Result.pages_committed + int(page),
        )
    )
    if result.rowcount != 1:
        raise RuntimeError("Reconciliation result is not running")


async def begin_run(sessions, run_id, start, end, started, data_types) -> None:
    async with sessions.begin() as session:
        session.add(Run(id=run_id, requested_from=start, requested_to=end, started_at=started))
        await session.flush()
        session.add_all(Result(run_id=run_id, data_type=name) for name in data_types)


async def start_type(sessions, run_id, data_type) -> None:
    async with sessions.begin() as session:
        row = await session.get(Result, (run_id, data_type))
        row.status, row.started_at = "running", datetime.now(UTC)


async def finish_type(sessions, run_id, data_type, failure: Exception | None = None) -> None:
    async with sessions.begin() as session:
        row = await session.get(Result, (run_id, data_type))
        row.status = (
            "completed"
            if failure is None
            else (
                "partial"
                if row.pages_committed or isinstance(failure, PaginationPaused)
                else "failed"
            )
        )
        row.error_type = type(failure).__name__[:100] if failure is not None else None
        row.finished_at = datetime.now(UTC)


async def finish_run(sessions, run_id, failure: BaseException | None = None) -> str:
    import asyncio

    async with sessions.begin() as session:
        run = await session.get(Run, run_id)
        rows = list((await session.scalars(select(Result).where(Result.run_id == run_id))).all())
        now = datetime.now(UTC)
        cancelled = isinstance(failure, asyncio.CancelledError)
        for row in rows:
            if row.status in {"pending", "running"}:
                row.status = "cancelled" if cancelled else "failed"
                row.error_type = type(failure).__name__[:100] if failure else "IncompleteResult"
                row.finished_at = now
        failures = [row for row in rows if row.status != "completed"]
        if cancelled:
            run.status = "cancelled"
        elif failures or failure:
            progress = any(row.pages_committed or row.status == "completed" for row in rows)
            run.status = "partial" if progress else "failed"
        else:
            run.status = "completed"
        run.error_type = (
            type(failure).__name__[:100]
            if failure
            else next((row.error_type for row in sorted(failures, key=lambda r: r.data_type)), None)
        )
        run.finished_at = now
        return run.status


async def recovery_errors(sessions, run_id: uuid.UUID) -> tuple[str, ...]:
    """Read the exact recovery invocation, never a latest-run pointer or an in-memory result."""
    async with sessions() as session:
        run = await session.get(Run, run_id)
        if run is None:
            return ("MissingReconciliationReport",)
        rows = list((await session.scalars(select(Result).where(Result.run_id == run_id))).all())
        if (
            run.status == "completed"
            and run.finished_at is not None
            and run.error_type is None
            and rows
            and all(
                row.status == "completed" and row.finished_at is not None and row.error_type is None
                for row in rows
            )
        ):
            return ()
        errors = tuple(sorted({row.error_type for row in rows if row.error_type}))
        return errors or (run.error_type or "IncompleteReconciliationReport",)


async def list_runs(session, offset: int = 0, limit: int = 20) -> dict:
    if not 0 <= offset <= 10000 or not 1 <= limit <= 100:
        raise ValueError("Invalid reconciliation pagination")
    runs = list(
        (
            await session.scalars(
                select(Run)
                .order_by(Run.started_at.desc(), Run.id.desc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    results: dict[uuid.UUID, list[dict]] = {run.id: [] for run in runs}
    if runs:
        rows = await session.scalars(
            select(Result)
            .where(Result.run_id.in_(results))
            .order_by(Result.run_id, Result.data_type)
        )
        for row in rows:
            results[row.run_id].append(
                {
                    column.name: getattr(row, column.name)
                    for column in Result.__table__.columns
                    if column.name != "run_id"
                }
            )
    return {
        "runs": [
            {
                **{column.name: getattr(run, column.name) for column in Run.__table__.columns},
                "types": results[run.id],
            }
            for run in runs
        ],
        "offset": offset,
        "limit": limit,
        "total": await session.scalar(select(func.count()).select_from(Run)),
    }
