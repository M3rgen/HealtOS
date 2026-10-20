from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, time, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, case, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.core.logging import event
from app.core.timeutil import as_utc
from app.google_health.client import GoogleHealthClient
from app.google_health.data_types import DataTypeSpec
from app.google_health.pagination import iter_pages
from app.models.state import BackfillJob, JobStatus
from app.services.storage import store_records
from app.sync.errors import error_summary

logger = logging.getLogger(__name__)

# A window that keeps failing (e.g. a 403 for one range) must not stall a whole data
# type: after this many failures it is tried only after every other window ...
DEPRIORITIZE_AFTER_RETRIES = 5
# ... and after this many it is parked as `abandoned` and shown in /backfill/status.
ABANDON_AFTER_RETRIES = 10


def time_windows(start: datetime, end: datetime, days: int):
    cursor = start
    delta = timedelta(days=days)
    while cursor < end:
        next_cursor = min(cursor + delta, end)
        yield cursor, next_cursor
        cursor = next_cursor


class BackfillService:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker,
        client: GoogleHealthClient,
        specs: dict[str, DataTypeSpec],
    ):
        self.settings = settings
        self.sessions = sessions
        self.client = client
        self.specs = specs
        self._run_lock = asyncio.Lock()

    async def ensure_jobs(self, data_types: list[str] | None = None) -> int:
        end = datetime.now(UTC)
        configured_start = datetime.combine(self.settings.backfill_start_date, time.min, tzinfo=UTC)
        created = 0
        for data_type in data_types or list(self.specs):
            if data_type not in self.specs:
                raise ValueError(f"Unsupported data type: {data_type}")
            async with self.sessions() as session:
                boundaries = (
                    await session.execute(
                        select(
                            func.min(BackfillJob.window_start),
                            func.max(BackfillJob.window_end),
                        ).where(BackfillJob.data_type == data_type)
                    )
                ).one()
                first_boundary, last_boundary = boundaries
                unfinished = await session.scalar(
                    select(func.count())
                    .select_from(BackfillJob)
                    .where(
                        BackfillJob.data_type == data_type,
                        BackfillJob.status.not_in([JobStatus.completed, JobStatus.abandoned]),
                    )
                )
            first_boundary, last_boundary = as_utc(first_boundary), as_utc(last_boundary)
            ranges = []
            if first_boundary is None:
                ranges.append((configured_start, end))
            elif not self.specs[data_type].time_filtered:
                # Reference catalogs have no time filter and every request returns
                # the current complete collection. One durable snapshot job is enough.
                continue
            else:
                if configured_start < first_boundary:
                    ranges.append((configured_start, first_boundary))
                if last_boundary < end and not unfinished:
                    ranges.append((last_boundary, end))
            for range_start, range_end in ranges:
                spec = self.specs[data_type]
                window_days = min(
                    self.settings.backfill_window_days,
                    spec.max_window_days or self.settings.backfill_window_days,
                )
                if spec.name == "electrocardiogram":
                    window_days = max(1, (range_end - range_start).days + 1)
                windows = (
                    time_windows(range_start, range_end, window_days)
                    if spec.time_filtered
                    else [(range_start, range_end)]
                )
                for window_start, window_end in windows:
                    async with self.sessions.begin() as session:
                        existing = await session.scalar(
                            select(BackfillJob.id).where(
                                BackfillJob.data_type == data_type,
                                BackfillJob.window_start == window_start,
                                BackfillJob.window_end == window_end,
                            )
                        )
                        if existing is None:
                            session.add(
                                BackfillJob(
                                    data_type=data_type,
                                    window_start=window_start,
                                    window_end=window_end,
                                )
                            )
                            created += 1
        return created

    async def reset_abandoned(self, data_types: list[str] | None = None) -> int:
        """Requeue abandoned windows (all, or of the given types) as fresh failures.

        For use once the cause is fixed: retry_count restarts at zero, so the window is
        tried in normal order again. A saved page token and last_error are kept; the
        next attempt resumes the window and replaces the error.
        """
        query = update(BackfillJob).where(BackfillJob.status == JobStatus.abandoned)
        if data_types:
            query = query.where(BackfillJob.data_type.in_(data_types))
        async with self.sessions.begin() as session:
            result = await session.execute(
                query.values(status=JobStatus.failed, retry_count=0).execution_options(
                    synchronize_session=False
                )
            )
        reset = cast(CursorResult[Any], result).rowcount or 0
        if reset:
            event(logger, "BACKFILL_ABANDONED_RESET", data_types=data_types or "all", count=reset)
        return reset

    async def run(self, data_types: list[str] | None = None) -> int:
        async with self._run_lock:
            return await self._run(data_types)

    async def _run(self, data_types: list[str] | None = None) -> int:
        await self.ensure_jobs(data_types)
        event(logger, "BACKFILL_STARTED", data_types=data_types or "all")
        selected = data_types or list(self.specs)
        bind = self.sessions.kw.get("bind")
        concurrency = (
            self.settings.sync_concurrency
            if bind is not None and bind.dialect.name == "postgresql"
            else 1
        )
        semaphore = asyncio.Semaphore(concurrency)

        async def worker(data_type: str) -> int:
            async with semaphore:
                return await self._run_type(data_type)

        return sum(await asyncio.gather(*(worker(data_type) for data_type in selected)))

    async def _run_type(self, data_type: str) -> int:
        completed = 0
        while True:
            async with self.sessions() as session:
                query = (
                    select(BackfillJob)
                    .where(
                        BackfillJob.status.in_(
                            [JobStatus.pending, JobStatus.failed, JobStatus.running]
                        )
                    )
                    .order_by(
                        case(
                            (BackfillJob.retry_count >= DEPRIORITIZE_AFTER_RETRIES, 1),
                            else_=0,
                        ),
                        BackfillJob.window_start.desc(),
                    )
                    .limit(1)
                    .where(BackfillJob.data_type == data_type)
                )
                job = await session.scalar(query)
            if job is None:
                return completed
            try:
                await self._run_job(job.id)
                completed += 1
            except Exception:
                logger.exception(
                    "Backfill data type paused after a failed window",
                    extra={"event": "BACKFILL_WINDOW_FAILED", "data_type": job.data_type},
                )
                return completed

    async def _run_job(self, job_id) -> None:
        async with self.sessions.begin() as session:
            job = await session.get(BackfillJob, job_id)
            job.status = JobStatus.running
            job.started_at = job.started_at or datetime.now(UTC)
            job.last_error = None
            spec = self.specs[job.data_type]
            start, end, token = job.window_start, job.window_end, job.page_token
        event(logger, "BACKFILL_WINDOW_STARTED", data_type=spec.name, window_start=str(start))
        try:
            async for page in iter_pages(self.client, spec, start, end, token):
                async with self.sessions.begin() as session:
                    await store_records(session, spec, page.data_points, source="backfill")
                    job = await session.get(BackfillJob, job_id)
                    job.page_token = page.next_page_token
                    job.records_downloaded += len(page.data_points)
            async with self.sessions.begin() as session:
                job = await session.get(BackfillJob, job_id)
                job.status = JobStatus.completed
                job.completed_at = datetime.now(UTC)
                job.page_token = None
            event(logger, "BACKFILL_WINDOW_COMPLETED", data_type=spec.name, window_start=str(start))
        except Exception as exc:
            async with self.sessions.begin() as session:
                job = await session.get(BackfillJob, job_id)
                job.retry_count += 1
                abandoned = job.retry_count >= ABANDON_AFTER_RETRIES
                job.status = JobStatus.abandoned if abandoned else JobStatus.failed
                job.last_error = error_summary(exc)
                retries = job.retry_count
            if abandoned:
                event(
                    logger,
                    "BACKFILL_WINDOW_ABANDONED",
                    data_type=spec.name,
                    window_start=str(start),
                    retry_count=retries,
                    error_type=type(exc).__name__,
                )
            raise
