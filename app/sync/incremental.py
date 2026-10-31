from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.core.logging import event
from app.core.provenance import current_context_id, new_context_id, provenance
from app.core.timeutil import as_utc
from app.google_health.client import GoogleHealthClient
from app.google_health.data_types import DataTypeSpec
from app.google_health.pagination import PageBudget, PaginationPaused, iter_pages
from app.models.state import JobStatus, SyncState, fetch_round_start
from app.services.reconciliation_reports import record_changes, reporting
from app.services.retrieval import ReceiptWriter
from app.services.storage import StoreStats, store_page
from app.sync.checkpoints import incremental_start
from app.sync.deletions import mark_source_deletions
from app.sync.errors import error_code, error_summary, is_transient
from app.sync.ownership import CheckedSessions, OwnershipLost, run_ownership

logger = logging.getLogger(__name__)

INTERRUPTED_ERROR = "process restarted"
# Heartbeat age selects recovery candidates; ownership decides whether they are abandoned.
STALE_RUNNING_AFTER = timedelta(minutes=15)


def stale_running_cutoff(now: datetime | None = None) -> datetime:
    """`running` rows last updated before this instant are candidates for recovery."""
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
        # In-memory per-run outcomes so clients can tell when a run they started has finished.
        self.sequence = 0
        self.last_runs: dict[str, dict] = {}

    async def recover_interrupted(self, now: datetime | None = None) -> int:
        """Fail stale states only when no run still owns the data type.

        Recheck the heartbeat under nonblocking run ownership. Keep the durable page
        token so a later invocation can finish the interrupted window first.
        """
        cutoff = stale_running_cutoff(now)
        async with self.sessions() as session:
            candidates = list(
                await session.scalars(
                    select(SyncState.data_type).where(
                        SyncState.status == JobStatus.running, SyncState.updated_at < cutoff
                    )
                )
            )
        recovered = 0
        for data_type in candidates:
            async with run_ownership(self.sessions, data_type, wait=False) as sessions:
                if sessions is None:
                    continue
                async with sessions.begin() as session:
                    result = await session.execute(
                        update(SyncState)
                        .where(
                            SyncState.data_type == data_type,
                            SyncState.status == JobStatus.running,
                            SyncState.updated_at < cutoff,
                        )
                        .values(status=JobStatus.failed, last_error=INTERRUPTED_ERROR)
                    )
                recovered += cast(CursorResult[Any], result).rowcount or 0
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
        *,
        check_owner: Callable[[], Awaitable[None]] | None = None,
    ) -> StoreStats:
        """Fetch a window; an interrupted earlier window is finished first.

        A saved page token belongs to the previously requested window. That window is
        completed as a first round, and the window requested by this call always runs
        afterwards as its own round, so a caller (e.g. a webhook element) only sees
        success once its own range was fetched. Responses are tagged with the caller's
        run id (webhook element, reconciliation) or a new ``sync:<uuid>``.
        """
        with provenance(context_id=current_context_id() or new_context_id("sync")):
            return await self._run(data_type, requested_start, requested_end, check_owner)

    async def _run(
        self,
        data_type: str,
        requested_start: datetime | None,
        requested_end: datetime | None,
        check_owner: Callable[[], Awaitable[None]] | None = None,
    ) -> StoreStats:
        if data_type not in self.specs:
            raise ValueError(f"Unsupported data type: {data_type}")
        async with run_ownership(self.sessions, data_type) as sessions:
            assert sessions is not None  # A waiting acquisition always owns the run.
            if check_owner is not None:
                sessions = CheckedSessions(sessions, check_owner)
            spec = self.specs[data_type]
            request_end = requested_end or datetime.now(UTC)
            async with sessions.begin() as session:
                state = await session.scalar(
                    select(SyncState).where(SyncState.data_type == data_type)
                )
                if state is None:
                    state = SyncState(data_type=data_type)
                    session.add(state)
                rounds: list[tuple[datetime, datetime, str | None]] = []
                # Finishing a foreign checkpoint can leave no budget for this window's
                # first page. Its pending range must still precede the next caller's.
                if (
                    (state.last_page_token or state.status == JobStatus.pending)
                    and state.last_requested_from
                    and state.last_requested_to
                ):
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
            budget = PageBudget()
            try:
                for round_start, round_end, page_token in rounds:
                    # Finish a foreign checkpoint without attributing its differences to
                    # this reconciliation's requested range. Keep the public stats contract.
                    scope = (
                        nullcontext() if (round_start, round_end) == requested else reporting(None)
                    )
                    with scope:
                        await self._fetch_round(
                            spec,
                            round_start,
                            round_end,
                            page_token,
                            total,
                            data_type,
                            sessions,
                            budget,
                        )
                async with sessions.begin() as session:
                    state = await session.get(SyncState, data_type)
                    assert state is not None
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
            except PaginationPaused:
                async with sessions.begin() as session:
                    state = await session.get(SyncState, data_type)
                    assert state is not None
                    state.status = JobStatus.pending
                    state.last_error = None
                # Preserve the existing outcome contract: an incomplete invocation is
                # never reported as success, even though its pages committed normally.
                self._finish(data_type, total, failed=True)
                event(logger, "SYNC_PAUSED", data_type=data_type)
                raise
            except Exception as exc:
                try:
                    async with sessions.begin() as session:
                        state = await session.get(SyncState, data_type)
                        assert state is not None
                        state.status = JobStatus.failed
                        state.last_error = error_summary(exc)
                except OwnershipLost:
                    # A replacement owner may already be resuming; never fail its state.
                    pass
                self._finish(data_type, total, failed=True)
                event(
                    logger,
                    "SYNC_FAILED",
                    data_type=data_type,
                    error_type=type(exc).__name__,
                    error_code=error_code(exc),
                    transient=is_transient(exc),
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
        sessions,
        budget: PageBudget,
    ) -> None:
        async with sessions.begin() as session:
            state = await session.get(SyncState, data_type)
            state.last_requested_from = start
            state.last_requested_to = end
            state.last_page_token = page_token
            # A spent budget only records this range as pending: no fetch start or
            # receipt is begun for a round that cannot request its first page.
            paused = budget.remaining <= 0
            if not paused:
                # A resumed round keeps the time its first page was fetched: "seen in
                # this fetch" always means since the round's real start.
                round_started = as_utc(
                    await fetch_round_start(
                        session, "sync", data_type, fresh=page_token is None, now=datetime.now(UTC)
                    )
                )
        if paused:
            raise PaginationPaused("Google Health page budget exhausted")
        # Only a fetch that starts at the first page sees the whole window, so only
        # then can records that stopped appearing be marked as deleted at the source.
        fetched_since = round_started if page_token is None else None
        receipt = ReceiptWriter(sessions, spec, start, end, "sync", data_type, page_token)
        await receipt.begin()
        try:
            await self._store_round(
                spec,
                start,
                end,
                page_token,
                total,
                data_type,
                round_started,
                fetched_since,
                receipt,
                sessions,
                budget,
            )
        except BaseException as failure:
            await receipt.failure(failure)
            raise

    async def _store_round(
        self,
        spec,
        start,
        end,
        page_token,
        total,
        data_type,
        round_started,
        fetched_since,
        receipt,
        sessions,
        budget,
    ) -> None:
        async def restart() -> None:
            nonlocal round_started, fetched_since
            round_started = await receipt.reset()
            # Even a saved-token continuation now fetches the entire window. Only
            # sightings since this new attempt can prevent source-deletion marks.
            fetched_since = round_started

        async for page in iter_pages(
            self.client, spec, start, end, page_token, on_reset=restart, budget=budget
        ):
            try:
                async with sessions.begin() as session:
                    page_stats = await store_page(session, spec, page, fetch_started=round_started)
                    state = await session.get(SyncState, data_type)
                    state.last_page_token = page.next_page_token
                    state.records_downloaded += len(page.data_points)
                    # Heartbeat per page; run ownership also protects slow network waits.
                    state.updated_at = datetime.now(UTC)
                    await receipt.page(session, page)
                    await record_changes(session, data_type, page_stats, page=True)
                total.add(page_stats)
                receipt.committed(page)
            except BaseException as failure:
                receipt.unstored = page
                # The page's records rolled back; what Google sent is still kept, also when
                # the task was cancelled (restart, deploy) between fetching and storing.
                await self.client.keep_after(page.responses, failure)
                raise
        async with sessions.begin() as session:
            state = await session.get(SyncState, data_type)
            previous_success = state.last_successful_sync
            # A widened webhook window may end in the future; the watermark never does.
            reached = min(end, datetime.now(UTC))
            state.last_successful_sync = (
                max(as_utc(previous_success), reached) if previous_success else reached
            )
            state.last_page_token = None
            deleted = 0
            if fetched_since is not None:
                deleted = await mark_source_deletions(session, spec, start, end, fetched_since)
            await record_changes(session, data_type, StoreStats(source_deleted=deleted))
            receipt.event(session, "completed")
        total.source_deleted += deleted

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
