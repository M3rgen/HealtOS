from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.logging import event
from app.services.pipeline_health import update_reliability_state
from app.services.storage import StoreStats, recording_source
from app.sync.incremental import IncrementalSync

logger = logging.getLogger(__name__)


class ReconciliationService:
    def __init__(
        self,
        incremental: IncrementalSync,
        sessions: async_sessionmaker | None = None,
    ):
        self.incremental = incremental
        self.sessions = sessions
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def run(self, days: int) -> dict:
        async with self._lock:
            with recording_source("reconciliation"):
                return await self._run(days)

    async def _run(self, days: int) -> dict:
        started = datetime.now(UTC)
        if self.sessions:
            await update_reliability_state(self.sessions, last_reconciliation_started_at=started)
        event(logger, "RECONCILIATION_STARTED", days=days)
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        results = {}
        for data_type, spec in self.incremental.specs.items():
            try:
                max_days = getattr(spec, "max_window_days", None)
                if not max_days or days <= max_days:
                    results[data_type] = await self.incremental.run(data_type, start)
                    continue
                aggregate = StoreStats()
                cursor = start
                while cursor < end:
                    window_end = min(cursor + timedelta(days=max_days), end)
                    aggregate.add(await self.incremental.run(data_type, cursor, window_end))
                    cursor = window_end
                results[data_type] = aggregate
            except Exception as exc:
                results[data_type] = exc

        failures = sum(isinstance(value, Exception) for value in results.values())
        completed = datetime.now(UTC)
        if self.sessions:
            if failures:
                await update_reliability_state(
                    self.sessions, last_reconciliation_failure_at=completed
                )
            else:
                await update_reliability_state(
                    self.sessions, last_reconciliation_success_at=completed
                )
        event(
            logger,
            "RECONCILIATION_COMPLETED",
            days=days,
            failure_count=failures,
            duration_seconds=round((completed - started).total_seconds(), 3),
        )
        return results
