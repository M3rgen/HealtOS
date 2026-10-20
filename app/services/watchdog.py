from __future__ import annotations

import asyncio
import logging
import math
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.core.logging import event
from app.core.timeutil import as_utc
from app.models.state import JobStatus, ReliabilityState, SyncState
from app.services.pipeline_health import (
    PipelineHealthService,
    PipelineSnapshot,
    PipelineStatus,
    update_reliability_state,
)
from app.sync.incremental import stale_running_cutoff
from app.sync.reconciliation import ReconciliationService

logger = logging.getLogger(__name__)

_ADVISORY_LOCK_ID = 7_104_201_986


@dataclass(frozen=True)
class RecoveryOutcome:
    action: str
    trigger: str
    days: int | None = None
    error_types: tuple[str, ...] = ()


class PipelineWatchdog:
    """Local-state watchdog with bounded, deduplicated reconciliation recovery."""

    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker,
        health: PipelineHealthService,
        reconciliation: ReconciliationService,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.health = health
        self.reconciliation = reconciliation
        self._recovery_lock = asyncio.Lock()
        self._last_status: PipelineStatus | None = None

    async def periodic_check(self) -> RecoveryOutcome:
        return await self.check(trigger="watchdog")

    async def startup_check(self) -> RecoveryOutcome:
        return await self.check(trigger="startup")

    async def check(self, *, trigger: str, now: datetime | None = None) -> RecoveryOutcome:
        now = as_utc(now or datetime.now(UTC))
        snapshot = await self.health.inspect(now=now, scheduler_running=True)
        self._log_state_change(snapshot)

        gap = _recovery_gap(snapshot, now)
        if trigger == "startup":
            threshold = timedelta(minutes=self.settings.startup_gap_threshold_minutes)
            should_recover = gap is not None and gap >= threshold
        else:
            should_recover = snapshot.status == PipelineStatus.stale

        if not should_recover:
            event(logger, "WATCHDOG_CHECK_COMPLETED", trigger=trigger, status=snapshot.status.value)
            return RecoveryOutcome("not_needed", trigger)
        if not self.settings.automatic_recovery_enabled:
            event(
                logger,
                "WATCHDOG_RECOVERY_DISABLED",
                trigger=trigger,
                status=snapshot.status.value,
            )
            return RecoveryOutcome("disabled", trigger)
        return await self._recover(trigger, gap or timedelta(0), now)

    async def _recover(self, trigger: str, gap: timedelta, now: datetime) -> RecoveryOutcome:
        if self._recovery_lock.locked() or self.reconciliation.running:
            event(logger, "WATCHDOG_RECOVERY_SKIPPED", trigger=trigger, reason="overlap")
            return RecoveryOutcome("overlap", trigger)

        async with self._recovery_lock:
            async with self._global_lock() as acquired:
                if not acquired:
                    event(
                        logger, "WATCHDOG_RECOVERY_SKIPPED", trigger=trigger, reason="global_lock"
                    )
                    return RecoveryOutcome("overlap", trigger)
                if await self._cooldown_active(now):
                    event(logger, "WATCHDOG_RECOVERY_SKIPPED", trigger=trigger, reason="cooldown")
                    return RecoveryOutcome("cooldown", trigger)
                if await self._sync_running(now):
                    event(
                        logger, "WATCHDOG_RECOVERY_SKIPPED", trigger=trigger, reason="sync_running"
                    )
                    return RecoveryOutcome("sync_running", trigger)

                days = min(
                    self.settings.automatic_recovery_max_days,
                    max(1, math.ceil(gap.total_seconds() / 86_400)),
                )
                await update_reliability_state(
                    self.sessions,
                    last_recovery_attempt_at=now,
                    last_recovery_error=None,
                )
                event(logger, "WATCHDOG_RECOVERY_STARTED", trigger=trigger, days=days)
                try:
                    results = await self.reconciliation.run(days)
                except Exception as exc:
                    await self._record_failure(now, (type(exc).__name__,))
                    event(
                        logger,
                        "WATCHDOG_RECOVERY_FAILED",
                        trigger=trigger,
                        days=days,
                        error_type=type(exc).__name__,
                    )
                    return RecoveryOutcome("failed", trigger, days, (type(exc).__name__,))

                errors = tuple(
                    sorted(
                        type(value).__name__
                        for value in results.values()
                        if isinstance(value, Exception)
                    )
                )
                if errors:
                    await self._record_failure(now, errors)
                    event(
                        logger,
                        "WATCHDOG_RECOVERY_FAILED",
                        trigger=trigger,
                        days=days,
                        failure_count=len(errors),
                    )
                    return RecoveryOutcome("failed", trigger, days, errors)

                await update_reliability_state(
                    self.sessions,
                    last_recovery_success_at=datetime.now(UTC),
                    last_recovery_error=None,
                )
                event(logger, "WATCHDOG_RECOVERY_COMPLETED", trigger=trigger, days=days)
                return RecoveryOutcome("completed", trigger, days)

    async def _cooldown_active(self, now: datetime) -> bool:
        async with self.sessions() as session:
            state = await session.get(ReliabilityState, 1)
        if state is None or state.last_recovery_attempt_at is None:
            return False
        attempted = as_utc(state.last_recovery_attempt_at)
        return now - attempted < timedelta(minutes=self.settings.recovery_cooldown_minutes)

    async def _sync_running(self, now: datetime) -> bool:
        # A `running` row without a recent heartbeat is a dead run, not a live one.
        async with self.sessions() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(SyncState)
                .where(
                    SyncState.status == JobStatus.running,
                    SyncState.updated_at >= stale_running_cutoff(now),
                )
            )
        return bool(count)

    async def _record_failure(self, now: datetime, errors: tuple[str, ...]) -> None:
        await update_reliability_state(
            self.sessions,
            last_recovery_failure_at=now,
            last_recovery_error=",".join(errors)[:2_000],
        )

    @asynccontextmanager
    async def _global_lock(self):
        async with self.sessions() as session:
            if session.get_bind().dialect.name != "postgresql":
                yield True
                return
            acquired = bool(
                await session.scalar(
                    text("SELECT pg_try_advisory_lock(:lock_id)"),
                    {"lock_id": _ADVISORY_LOCK_ID},
                )
            )
            try:
                yield acquired
            finally:
                if acquired:
                    await session.execute(
                        text("SELECT pg_advisory_unlock(:lock_id)"),
                        {"lock_id": _ADVISORY_LOCK_ID},
                    )

    def _log_state_change(self, snapshot: PipelineSnapshot) -> None:
        if snapshot.status == self._last_status:
            return
        event(
            logger,
            "WATCHDOG_STATE_CHANGED",
            previous=self._last_status.value if self._last_status else None,
            current=snapshot.status.value,
            stale_stream_count=len(snapshot.stale_streams),
            pending_notifications=snapshot.pending_notifications,
            failed_notifications=snapshot.failed_notifications,
            failed_syncs=snapshot.failed_syncs,
        )
        self._last_status = snapshot.status


def _recovery_gap(snapshot: PipelineSnapshot, now: datetime) -> timedelta | None:
    reference = (
        snapshot.oldest_frequent_sync
        or snapshot.latest_frequent_sync
        or snapshot.latest_health_write
    )
    if reference is None:
        return None
    return max(timedelta(0), now - as_utc(reference))
