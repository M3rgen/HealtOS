from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.core.logging import event
from app.core.provenance import current_context_id, new_context_id, provenance
from app.core.timeutil import as_utc
from app.google_health.client import GoogleHealthClient
from app.google_health.data_types import DataTypeSpec
from app.google_health.pagination import iter_pages
from app.models.state import JobStatus, SyncState
from app.services.storage import StoreStats, store_page
from app.sync.checkpoints import incremental_start
from app.sync.deletions import mark_source_deletions
from app.sync.errors import error_code, error_summary

logger = logging.getLogger(__name__)

INTERRUPTED_ERROR = "process restarted"
# A run refreshes SyncState.updated_at on every page, so a `running` row untouched for this
# long has no live owner. A fresher one may belong to another live process (`healthos sync`
# in a separate container) and must not be failed underneath it.
STALE_RUNNING_AFTER = timedelta(minutes=15)


def stale_running_cutoff(now: datetime | None = None) -> datetime:
    """`running` rows last updated before this instant are treated as abandoned."""
    return (now or datetime.now(UTC)) - STALE_RUNNING_AFTER


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

    async def recover_interrupted(self, now: datetime | None = None) -> int:
        """Fail sync states left `running` by a process that died mid-run.

        Only rows without a heartbeat for STALE_RUNNING_AFTER count: a fresher row may be
        a live run in another process. Called at startup and once more after that grace
        period. The saved page token is kept, so the next run resumes the window.
        """
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(SyncState)
                .where(
                    SyncState.status == JobStatus.running,
                    SyncState.updated_at < stale_running_cutoff(now),
                )
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
        """Fetch a window; an interrupted earlier window is finished first.

        A saved page token belongs to the previously requested window. That window is
        completed as a first round, and the window requested by this call always runs
        afterwards as its own round, so a caller (e.g. a webhook element) only sees
        success once its own range was fetched. Responses are tagged with the caller's
        run id (webhook element, reconciliation) or a new ``sync:<uuid>``.
        """
        with provenance(context_id=current_context_id() or new_context_id("sync")):
            return await self._run(data_type, requested_start, requested_end)

    async def _run(
        self,
        data_type: str,
        requested_start: datetime | None,
        requested_end: datetime | None,
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
                rounds: list[tuple[datetime, datetime, str | None]] = []
                if state.last_page_token and state.last_requested_from and state.last_requested_to:
                    rounds.append(
                        (
                            as_utc(state.last_requested_from),
                            as_utc(state.last_requested_to),
                            state.last_page_token,
                        )
                    )
                request_start = requested_start or incremental_start(
                    state.last_successful_sync,
                    self.settings.safety_window(data_type),
                    request_end,
                )
                requested = (as_utc(request_start), as_utc(request_end))
                if not rounds or rounds[0][:2] != requested:
                    rounds.append((*requested, None))
                state.status = JobStatus.running
                state.last_error = None
            event(
                logger,
                "SYNC_STARTED",
                data_type=data_type,
                requested_from=str(request_start),
                resumed=rounds[0][2] is not None,
            )
            total = StoreStats()
            try:
                for round_start, round_end, page_token in rounds:
                    await self._fetch_round(
                        spec, round_start, round_end, page_token, total, data_type
                    )
                async with self.sessions.begin() as session:
                    state = await session.get(SyncState, data_type)
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
                    state.last_error = error_summary(exc)
                self._finish(data_type, total, failed=True)
                event(
                    logger,
                    "SYNC_FAILED",
                    data_type=data_type,
                    error_type=type(exc).__name__,
                    error_code=error_code(exc),
                )
                raise

    async def _fetch_round(
        self,
        spec: DataTypeSpec,
        start: datetime,
        end: datetime,
        page_token: str | None,
        total: StoreStats,
        data_type: str,
    ) -> None:
        async with self.sessions.begin() as session:
            state = await session.get(SyncState, data_type)
            state.last_requested_from = start
            state.last_requested_to = end
            state.last_page_token = page_token
        # Only a fetch that starts at the first page sees the whole window, so only
        # then can records that stopped appearing be marked as deleted at the source.
        fetched_since = datetime.now(UTC) if page_token is None else None
        async for page in iter_pages(self.client, spec, start, end, page_token):
            try:
                async with self.sessions.begin() as session:
                    total.add(await store_page(session, spec, page))
                    state = await session.get(SyncState, data_type)
                    state.last_page_token = page.next_page_token
                    state.records_downloaded += len(page.data_points)
                    # Heartbeat per page, so recovery never fails a live run.
                    state.updated_at = datetime.now(UTC)
            except Exception:
                # The page's records rolled back; what Google sent is still kept.
                await self.client.keep(page.responses)
                raise
        async with self.sessions.begin() as session:
            state = await session.get(SyncState, data_type)
            previous_success = state.last_successful_sync
            # A widened webhook window may end in the future; the watermark never does.
            reached = min(end, datetime.now(UTC))
            state.last_successful_sync = (
                max(as_utc(previous_success), reached) if previous_success else reached
            )
            state.last_page_token = None
            if fetched_since is not None:
                total.source_deleted += await mark_source_deletions(
                    session, spec, start, end, fetched_since
                )

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
