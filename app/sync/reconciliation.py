from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.logging import event
from app.core.provenance import provenance
from app.google_health.client import RetryDeferredError
from app.google_health.pagination import PaginationPaused
from app.services import reconciliation_reports as reports
from app.services.pipeline_health import update_reliability_state
from app.services.storage import StoreStats
from app.sync.errors import is_transient
from app.sync.incremental import IncrementalSync

logger = logging.getLogger(__name__)

FINAL_PASS_DELAY_SECONDS = 5.0


class ReconciliationService:
    def __init__(
        self,
        incremental: IncrementalSync,
        sessions: async_sessionmaker | None = None,
        sleep=asyncio.sleep,
    ):
        self.incremental = incremental
        self.sleep = sleep
        self.sessions = sessions if sessions is not None else getattr(incremental, "sessions", None)
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def run(self, days: int, *, run_id: uuid.UUID | None = None) -> dict:
        if days <= 0:
            raise ValueError("Reconciliation days must be positive")
        async with self._lock:
            run_id = run_id or uuid.uuid4()
            with provenance(source="reconciliation", context_id=f"reconciliation:{run_id}"):
                return await self._run(days, run_id)

    async def _run(self, days: int, run_id: uuid.UUID) -> dict:
        started = datetime.now(UTC)
        end = started
        start = end - timedelta(days=days)
        if self.sessions:
            await reports.begin_run(
                self.sessions, run_id, start, end, started, self.incremental.specs
            )
        try:
            results = await self._fetch(days, start, end, run_id)
            if self.sessions:
                status = await reports.finish_run(self.sessions, run_id)
                field = (
                    "last_reconciliation_success_at"
                    if status == "completed"
                    else "last_reconciliation_failure_at"
                )
                await update_reliability_state(self.sessions, **{field: datetime.now(UTC)})
            return results
        except BaseException as failure:
            if self.sessions:
                # A repeated cancellation or failed outcome write leaves an incomplete report,
                # which readers and watchdog must never promote to success.
                task = asyncio.create_task(reports.finish_run(self.sessions, run_id, failure))
                try:
                    await asyncio.wait_for(asyncio.shield(task), 10)
                    await update_reliability_state(
                        self.sessions, last_reconciliation_failure_at=datetime.now(UTC)
                    )
                except BaseException:
                    task.cancel()
                    logger.warning("Reconciliation outcome was not persisted")
            raise

    async def _fetch(self, days, start, end, run_id) -> dict:
        if self.sessions:
            await update_reliability_state(self.sessions, last_reconciliation_started_at=end)
        event(logger, "RECONCILIATION_STARTED", days=days)
        # A shared credential failure must fail the run once, rather than repeating
        # the same refresh (and its backoff) for every configured data type.
        await self.incremental.client.warm_up()
        results = {}
        pending = list(self.incremental.specs)
        for final_pass in (False, True):
            if final_pass and pending:
                # Let a brief outage or rate limit pass before the one final attempt.
                await self.sleep(FINAL_PASS_DELAY_SECONDS)
            retry = []
            for data_type in pending:
                result = await self._fetch_type(data_type, days, start, end, run_id)
                results[data_type] = result
                # A deferred Retry-After forbids any request before Google's time.
                if isinstance(result, Exception) and is_transient(result):
                    if not isinstance(result, RetryDeferredError):
                        retry.append(data_type)
            pending = retry

        pauses = sum(isinstance(value, PaginationPaused) for value in results.values())
        failures = sum(isinstance(value, Exception) for value in results.values()) - pauses
        completed = datetime.now(UTC)
        event(
            logger,
            "RECONCILIATION_COMPLETED",
            days=days,
            failure_count=failures,
            paused_count=pauses,
            duration_seconds=round((completed - end).total_seconds(), 3),
        )
        return results

    async def _fetch_type(self, data_type, days, start, end, run_id):
        spec = self.incremental.specs[data_type]
        if self.sessions:
            await reports.start_type(self.sessions, run_id, data_type)
        failure = None
        try:
            with reports.reporting(run_id if self.sessions else None, data_type):
                max_days = getattr(spec, "max_window_days", None)
                if not max_days or days <= max_days:
                    result = await self.incremental.run(data_type, start, end)
                else:
                    result = StoreStats()
                    cursor = start
                    while cursor < end:
                        window_end = min(cursor + timedelta(days=max_days), end)
                        result.add(await self.incremental.run(data_type, cursor, window_end))
                        cursor = window_end
        except Exception as exc:
            result = failure = exc
        if self.sessions:
            await reports.finish_type(self.sessions, run_id, data_type, failure)
        return result
