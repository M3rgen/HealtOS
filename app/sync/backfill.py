from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from datetime import UTC, datetime, time, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, case, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.core.logging import event
from app.core.provenance import provenance
from app.core.timeutil import as_utc
from app.google_health.client import GoogleHealthClient
from app.google_health.data_types import DataTypeSpec
from app.google_health.pagination import PageBudget, PaginationPaused, iter_pages
from app.models.state import BackfillJob, JobStatus, fetch_round_start
from app.services.retrieval import ReceiptWriter
from app.services.storage import store_page
from app.sync.errors import error_summary
from app.sync.ownership import OwnershipLost, run_ownership

logger = logging.getLogger(__name__)

# A window that keeps failing (e.g. a 403 for one range) must not stall a whole data
# type: after this many failures it is tried only after every other window ...
DEPRIORITIZE_AFTER_RETRIES = 5
# ... and after this many it is parked as `abandoned` and shown in /backfill/status.
ABANDON_AFTER_RETRIES = 10
RUNNABLE_STATUSES = (JobStatus.pending, JobStatus.failed, JobStatus.running)


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

    async def _ensure_jobs(self, data_type, end, configured_start, sessions) -> int:
        """Read boundaries only after acquiring this type; never reacquire ownership."""
        created = 0
        async with sessions.begin() as session:
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
            return 0
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
                async with sessions.begin() as session:
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
        query = select(BackfillJob.data_type).where(BackfillJob.status == JobStatus.abandoned)
        if data_types:
            query = query.where(BackfillJob.data_type.in_(data_types))
        async with self.sessions() as session:
            selected = list(await session.scalars(query.distinct()))
        reset = 0
        for data_type in selected:
            async with run_ownership(self.sessions, data_type) as sessions:
                assert sessions is not None
                async with sessions.begin() as session:
                    result = await session.execute(
                        update(BackfillJob)
                        .where(
                            BackfillJob.data_type == data_type,
                            BackfillJob.status == JobStatus.abandoned,
                        )
                        .values(status=JobStatus.failed, retry_count=0)
                        .execution_options(synchronize_session=False)
                    )
                reset += cast(CursorResult[Any], result).rowcount or 0
        if reset:
            event(logger, "BACKFILL_ABANDONED_RESET", data_types=data_types or "all", count=reset)
        return reset

    async def run(self, data_types: list[str] | None = None) -> int:
        # Fix the planning horizon before waiting: a delayed worker only plans up to
        # its own invocation, then rechecks durable jobs under the type's ownership.
        end = datetime.now(UTC)
        event(logger, "BACKFILL_STARTED", data_types=data_types or "all")
        selected = data_types or list(self.specs)
        for data_type in selected:
            if data_type not in self.specs:
                raise ValueError(f"Unsupported data type: {data_type}")
        bind = self.sessions.kw.get("bind")
        concurrency = (
            self.settings.sync_concurrency
            if bind is not None and bind.dialect.name == "postgresql"
            else 1
        )
        slots = asyncio.Semaphore(concurrency)
        return sum(await asyncio.gather(*(self._run_type(name, end, slots) for name in selected)))

    async def _run_type(
        self, data_type: str, end: datetime | None = None, slots: asyncio.Semaphore | None = None
    ) -> int:
        configured_start = datetime.combine(self.settings.backfill_start_date, time.min, tzinfo=UTC)
        async with run_ownership(self.sessions, data_type) as sessions:
            assert sessions is not None
            await self._ensure_jobs(data_type, end or datetime.now(UTC), configured_start, sessions)
        completed = 0
        budget = PageBudget()
        # A spent budget ends the invocation before another window is selected.
        while budget.remaining > 0:
            try:
                outcome = await self._run_next_window(data_type, budget, slots)
            except PaginationPaused:
                return completed
            except Exception:
                logger.exception(
                    "Backfill data type paused after a failed window",
                    extra={"event": "BACKFILL_WINDOW_FAILED", "data_type": data_type},
                )
                return completed
            if outcome is None:
                return completed
            completed += outcome
        return completed

    async def _run_next_window(
        self, data_type: str, budget: PageBudget, slots: asyncio.Semaphore | None = None
    ) -> bool | None:
        """Select and fetch one window under ownership; None when none is runnable.

        Ownership spans selection to outcome, so no window runs twice across processes.
        It is released between windows: a waiting incremental sync or webhook gets the
        type in request order instead of after the whole history. A fetch slot is taken
        only once the type is owned, so waiting for a busy type never idles another.
        """
        async with run_ownership(self.sessions, data_type) as sessions:
            assert sessions is not None
            async with sessions.begin() as session:
                job_id = await session.scalar(
                    select(BackfillJob.id)
                    .where(
                        BackfillJob.data_type == data_type,
                        BackfillJob.status.in_(RUNNABLE_STATUSES),
                    )
                    .order_by(
                        case(
                            (BackfillJob.retry_count >= DEPRIORITIZE_AFTER_RETRIES, 1),
                            else_=0,
                        ),
                        BackfillJob.window_start.desc(),
                    )
                    .limit(1)
                )
            if job_id is None:
                return None
            async with slots or nullcontext():
                return await self._run_owned_job(job_id, sessions, budget)

    async def _run_owned_job(self, job_id, sessions, budget: PageBudget | None = None) -> bool:
        async with sessions.begin() as session:
            job = await session.get(BackfillJob, job_id)
            if job is None or job.status not in RUNNABLE_STATUSES:
                return False
            job.status = JobStatus.running
            job.started_at = job.started_at or datetime.now(UTC)
            job.last_error = None
            # A fetch resumed from its page token keeps the time its first page was fetched.
            run_started = as_utc(
                await fetch_round_start(
                    session,
                    "backfill",
                    str(job_id),
                    fresh=job.page_token is None,
                    now=datetime.now(UTC),
                )
            )
            spec = self.specs[job.data_type]
            start, end, token = job.window_start, job.window_end, job.page_token
        event(logger, "BACKFILL_WINDOW_STARTED", data_type=spec.name, window_start=str(start))
        receipt = ReceiptWriter(sessions, spec, start, end, "backfill", str(job_id), token)
        began = False

        async def restart() -> None:
            nonlocal run_started
            run_started = await receipt.reset()

        try:
            # Inside the try: a failed receipt insert is a failed attempt too, so the job
            # leaves `running` and its retry count advances toward abandonment.
            await receipt.begin()
            began = True
            with provenance(source="backfill", context_id=f"backfill:{job_id}"):
                async for page in iter_pages(
                    self.client, spec, start, end, token, on_reset=restart, budget=budget
                ):
                    try:
                        async with sessions.begin() as session:
                            await store_page(
                                session, spec, page, source="backfill", fetch_started=run_started
                            )
                            job = await session.get(BackfillJob, job_id)
                            if not page.cyclic:  # A repeated token is never a resume point.
                                job.page_token = page.next_page_token
                            job.records_downloaded += len(page.data_points)
                            await receipt.page(session, page)
                        receipt.committed(page)
                    except BaseException as failure:
                        receipt.received = page
                        # Also on cancellation: what Google sent is kept.
                        await self.client.keep_after(page.responses, failure)
                        raise
            async with sessions.begin() as session:
                job = await session.get(BackfillJob, job_id)
                job.status = JobStatus.completed
                job.completed_at = datetime.now(UTC)
                job.page_token = None
                receipt.event(session, "completed")
            event(logger, "BACKFILL_WINDOW_COMPLETED", data_type=spec.name, window_start=str(start))
        except PaginationPaused:
            # Keep the page pointer, fetch start and all committed evidence. A full
            # budget is normal progress and must never count toward abandonment.
            async with sessions.begin() as session:
                job = await session.get(BackfillJob, job_id)
                job.status = JobStatus.pending
                job.last_error = None
            event(logger, "BACKFILL_WINDOW_PAUSED", data_type=spec.name, window_start=str(start))
            raise
        except BaseException as exc:
            if began:  # without a receipt row there is no outcome to attach
                await receipt.failure(exc)
            if not isinstance(exc, Exception):
                raise
            try:
                async with sessions.begin() as session:
                    job = await session.get(BackfillJob, job_id)
                    job.retry_count += 1
                    abandoned = job.retry_count >= ABANDON_AFTER_RETRIES
                    job.status = JobStatus.abandoned if abandoned else JobStatus.failed
                    job.last_error = error_summary(exc)
                    retries = job.retry_count
            except OwnershipLost:
                # The replacement may already have completed this job. Never use the
                # application pool to write an outcome after losing this connection.
                raise exc from None
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
        return True
