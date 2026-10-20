from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.core.logging import event
from app.google_health.client import GoogleHealthClient
from app.google_health.data_types import DataTypeSpec
from app.google_health.pagination import iter_pages
from app.models.state import JobStatus, SyncState
from app.services.storage import StoreStats, store_records
from app.sync.checkpoints import incremental_start

logger = logging.getLogger(__name__)

INTERRUPTED_ERROR = "process restarted"


class IncrementalSync:
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
        self._locks: dict[str, asyncio.Lock] = {}
        # In-memory per-run outcomes so clients can tell when a run they started has finished.
        self.sequence = 0
        self.last_runs: dict[str, dict] = {}

    async def recover_interrupted(self) -> int:
        """Fail sync states left `running` by a process that died mid-run.

        Called once at startup, before any new run can start. The saved page token is
        kept, so the next run resumes the interrupted window.
        """
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(SyncState)
                .where(SyncState.status == JobStatus.running)
                .values(status=JobStatus.failed, last_error=INTERRUPTED_ERROR)
            )
        recovered = result.rowcount or 0
        if recovered:
            event(logger, "SYNC_STATE_RECOVERED", count=recovered)
        return recovered

    def _finish(self, data_type: str, stats: StoreStats, failed: bool) -> None:
        self.sequence += 1
        self.last_runs[data_type] = {
            "sequence": self.sequence,
            "inserted": stats.inserted,
            "updated": stats.updated,
            "failed": failed,
        }

    async def run(
        self,
        data_type: str,
        requested_start: datetime | None = None,
        requested_end: datetime | None = None,
    ) -> StoreStats:
        if data_type not in self.specs:
            raise ValueError(f"Unsupported data type: {data_type}")
        async with self._locks.setdefault(data_type, asyncio.Lock()):
            spec = self.specs[data_type]
            request_end = requested_end or datetime.now(UTC)
            async with self.sessions.begin() as session:
                state = await session.scalar(
                    select(SyncState).where(SyncState.data_type == data_type)
                )
                if state is None:
                    state = SyncState(data_type=data_type)
                    session.add(state)
                resume = (
                    state.last_page_token and state.last_requested_from and state.last_requested_to
                )
                request_start = (
                    state.last_requested_from
                    if resume
                    else requested_start
                    or incremental_start(
                        state.last_successful_sync,
                        self.settings.safety_window(data_type),
                        request_end,
                    )
                )
                if resume:
                    request_end = state.last_requested_to
                page_token = state.last_page_token if resume else None
                state.last_requested_from = request_start
                state.last_requested_to = request_end
                state.status = JobStatus.running
                state.last_error = None
            event(logger, "SYNC_STARTED", data_type=data_type, requested_from=str(request_start))
            total = StoreStats()
            try:
                async for page in iter_pages(
                    self.client, spec, request_start, request_end, page_token
                ):
                    async with self.sessions.begin() as session:
                        stats = await store_records(session, spec, page.data_points)
                        total.add(stats)
                        state = await session.get(SyncState, data_type)
                        state.last_page_token = page.next_page_token
                        state.records_downloaded += len(page.data_points)
                async with self.sessions.begin() as session:
                    state = await session.get(SyncState, data_type)
                    previous_success = state.last_successful_sync
                    if previous_success and previous_success.tzinfo is None:
                        previous_success = previous_success.replace(tzinfo=UTC)
                    state.last_successful_sync = (
                        max(previous_success, request_end) if previous_success else request_end
                    )
                    state.last_page_token = None
                    state.status = JobStatus.completed
                    state.last_error = None
                self._finish(data_type, total, failed=False)
                event(
                    logger,
                    "SYNC_COMPLETED",
                    data_type=data_type,
                    records=total.inserted + total.updated,
                )
                return total
            except Exception as exc:
                async with self.sessions.begin() as session:
                    state = await session.get(SyncState, data_type)
                    state.status = JobStatus.failed
                    state.last_error = str(exc)[:2000]
                self._finish(data_type, total, failed=True)
                event(
                    logger,
                    "SYNC_FAILED",
                    data_type=data_type,
                    error_type=type(exc).__name__,
                )
                raise

    async def run_all(self) -> dict[str, StoreStats | Exception]:
        semaphore = asyncio.Semaphore(self.settings.sync_concurrency)

        async def guarded(name: str):
            async with semaphore:
                try:
                    return await self.run(name)
                except Exception as exc:
                    return exc

        results = await asyncio.gather(*(guarded(name) for name in self.specs))
        return dict(zip(self.specs, results, strict=True))
