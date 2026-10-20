from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.timeutil import as_utc
from app.models.state import (
    RawHealthData,
    ReliabilityState,
    SyncState,
    WebhookEvent,
    WebhookNotification,
)


class PipelineStatus(StrEnum):
    healthy = "healthy"
    degraded = "degraded"
    stale = "stale"
    failed = "failed"


@dataclass(frozen=True)
class PipelineSnapshot:
    status: PipelineStatus
    checked_at: datetime
    last_webhook_received: datetime | None
    last_webhook_processed: datetime | None
    last_sync_success: datetime | None
    latest_frequent_sync: datetime | None
    latest_health_write: datetime | None
    last_reconciliation: datetime | None
    pending_notifications: int
    failed_notifications: int
    failed_syncs: int
    data_freshness: dict[str, datetime | None] = field(default_factory=dict)
    stale_streams: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    oldest_frequent_sync: datetime | None = None
    data_freshness_checked_at: datetime | None = None

    def as_dict(
        self,
        *,
        database: str = "healthy",
        scheduler: str = "running",
        configuration: str = "healthy",
    ) -> dict:
        return {
            "status": self.status.value,
            "database": database,
            "scheduler": scheduler,
            "configuration": configuration,
            "checked_at": _iso(self.checked_at),
            "last_webhook_received": _iso(self.last_webhook_received),
            "last_webhook_processed": _iso(self.last_webhook_processed),
            "last_sync_success": _iso(self.last_sync_success),
            "oldest_frequent_sync": _iso(self.oldest_frequent_sync),
            "last_reconciliation": _iso(self.last_reconciliation),
            "latest_health_write": _iso(self.latest_health_write),
            "data_freshness_checked_at": _iso(self.data_freshness_checked_at),
            "pending_notifications": self.pending_notifications,
            "failed_notifications": self.failed_notifications,
            "failed_syncs": self.failed_syncs,
            "stale_streams": list(self.stale_streams),
            "reasons": list(self.reasons),
            "data_freshness": {
                _public_name(name): _iso(value) for name, value in self.data_freshness.items()
            },
        }


@dataclass(frozen=True)
class FreshnessSummary:
    """Latest per-stream data time and last raw write, computed from raw_health_data."""

    data_freshness: dict[str, datetime | None]
    latest_health_write: datetime | None
    checked_at: datetime
    computed_monotonic: float


def effective_time_expression():
    """Data time of a raw record; matches ix_raw_health_data_type_freshness."""
    return func.coalesce(RawHealthData.source_end_time, RawHealthData.source_start_time)


class PipelineHealthService:
    """Derive pipeline health exclusively from persisted local state.

    Small state tables (sync_state, webhook tables, reliability_state) are read on every
    call. The raw_health_data freshness summary is the only part that touches the large
    table; it is cached and recomputed at most once per refresh interval (hourly by
    default, plus an hourly scheduler job), so frequent /health polling never scans it.
    """

    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker,
        frequent_data_types: set[str],
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.frequent_data_types = frequent_data_types
        self._freshness: FreshnessSummary | None = None
        self._freshness_lock = asyncio.Lock()

    @property
    def cached_freshness(self) -> FreshnessSummary | None:
        return self._freshness

    async def refresh_freshness(self, *, now: datetime | None = None) -> FreshnessSummary:
        """Recompute the raw_health_data freshness summary (hourly job / cache miss)."""
        async with self._freshness_lock:
            async with self.sessions() as session:
                return await self._compute_freshness(session, as_utc(now or datetime.now(UTC)))

    async def _freshness_summary(self, session: AsyncSession, now: datetime) -> FreshnessSummary:
        max_age = self.settings.pipeline_freshness_refresh_seconds
        cached = self._freshness
        if cached is not None and time.monotonic() - cached.computed_monotonic < max_age:
            return cached
        async with self._freshness_lock:
            cached = self._freshness
            if cached is not None and time.monotonic() - cached.computed_monotonic < max_age:
                return cached
            return await self._compute_freshness(session, now)

    async def _compute_freshness(self, session: AsyncSession, now: datetime) -> FreshnessSummary:
        # One index-backed "ORDER BY ... DESC LIMIT 1" probe per frequent stream
        # (ix_raw_health_data_type_freshness) instead of GROUP BY over the whole table.
        effective = effective_time_expression()
        names = sorted(self.frequent_data_types)
        data_freshness: dict[str, datetime | None] = {}
        if names:
            probes = [
                select(effective)
                .where(RawHealthData.data_type == name, effective.is_not(None))
                .order_by(effective.desc())
                .limit(1)
                .scalar_subquery()
                .label(f"t{index}")
                for index, name in enumerate(names)
            ]
            row = (await session.execute(select(*probes))).one()
            data_freshness = {
                name: as_utc(value)
                for name, value in zip(names, row, strict=True)
                if value is not None
            }
        latest_health_write = await session.scalar(select(func.max(RawHealthData.updated_at)))
        summary = FreshnessSummary(
            data_freshness=data_freshness,
            latest_health_write=as_utc(latest_health_write),
            checked_at=now,
            computed_monotonic=time.monotonic(),
        )
        self._freshness = summary
        return summary

    async def inspect(
        self,
        *,
        now: datetime | None = None,
        scheduler_running: bool = True,
        session: AsyncSession | None = None,
    ) -> PipelineSnapshot:
        now = as_utc(now or datetime.now(UTC))
        if session is not None:
            return await self._inspect(session, now, scheduler_running)
        async with self.sessions() as owned_session:
            return await self._inspect(owned_session, now, scheduler_running)

    async def _inspect(
        self, session: AsyncSession, now: datetime, scheduler_running: bool
    ) -> PipelineSnapshot:
        webhook = (
            await session.execute(
                select(
                    func.max(WebhookEvent.created_at),
                    func.max(WebhookEvent.processed_at),
                )
            )
        ).one()
        notification_counts = (
            await session.execute(
                select(
                    func.count().filter(WebhookNotification.status.in_(("pending", "running"))),
                    func.count().filter(WebhookNotification.status == "failed"),
                )
            )
        ).one()
        sync_summary = (
            await session.execute(
                select(
                    func.max(SyncState.last_successful_sync),
                    func.count().filter(
                        (SyncState.status == "failed") | SyncState.last_error.is_not(None)
                    ),
                )
            )
        ).one()
        sync_rows = (
            await session.execute(
                select(SyncState.data_type, SyncState.last_successful_sync).where(
                    SyncState.data_type.in_(self.frequent_data_types)
                )
            )
        ).all()
        freshness = await self._freshness_summary(session, now)
        reliability = await session.get(ReliabilityState, 1)

        sync_by_type = {name: as_utc(value) for name, value in sync_rows}
        stale_after = timedelta(minutes=self.settings.pipeline_stale_threshold_minutes)
        stale_streams = tuple(
            sorted(
                name
                for name in self.frequent_data_types
                if sync_by_type.get(name) is None or now - sync_by_type[name] >= stale_after
            )
        )
        latest_frequent_sync = max(
            (value for value in sync_by_type.values() if value is not None), default=None
        )
        oldest_frequent_sync = min(
            (value for value in sync_by_type.values() if value is not None), default=None
        )

        pending = int(notification_counts[0] or 0)
        failed_notifications = int(notification_counts[1] or 0)
        failed_syncs = int(sync_summary[1] or 0)
        reasons: list[str] = []
        status = PipelineStatus.healthy
        if failed_notifications or failed_syncs:
            status = PipelineStatus.failed
            if failed_notifications:
                reasons.append("failed_webhook_notifications")
            if failed_syncs:
                reasons.append("failed_sync_states")
        elif sync_by_type and len(stale_streams) == len(self.frequent_data_types):
            status = PipelineStatus.stale
            reasons.append("all_frequent_streams_stale")
        elif stale_streams:
            status = PipelineStatus.degraded
            reasons.append("some_frequent_streams_stale")
        elif not sync_by_type:
            status = PipelineStatus.degraded
            reasons.append("sync_state_uninitialized")

        if pending and status == PipelineStatus.healthy:
            status = PipelineStatus.degraded
            reasons.append("webhook_backlog")
        elif pending:
            reasons.append("webhook_backlog")
        if not scheduler_running:
            if status == PipelineStatus.healthy:
                status = PipelineStatus.degraded
            reasons.append("scheduler_not_running")

        return PipelineSnapshot(
            status=status,
            checked_at=now,
            last_webhook_received=as_utc(webhook[0]),
            last_webhook_processed=as_utc(webhook[1]),
            last_sync_success=as_utc(sync_summary[0]),
            latest_frequent_sync=latest_frequent_sync,
            latest_health_write=freshness.latest_health_write,
            last_reconciliation=(
                as_utc(reliability.last_reconciliation_success_at) if reliability else None
            ),
            pending_notifications=pending,
            failed_notifications=failed_notifications,
            failed_syncs=failed_syncs,
            data_freshness=dict(freshness.data_freshness),
            stale_streams=stale_streams,
            reasons=tuple(reasons),
            oldest_frequent_sync=oldest_frequent_sync,
            data_freshness_checked_at=freshness.checked_at,
        )


async def update_reliability_state(
    sessions: async_sessionmaker, **values: datetime | str | None
) -> ReliabilityState:
    async with sessions.begin() as session:
        state = await session.get(ReliabilityState, 1)
        if state is None:
            state = ReliabilityState(id=1)
            session.add(state)
        for name, value in values.items():
            setattr(state, name, value)
        return state


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _public_name(data_type: str) -> str:
    return {
        "heart-rate": "heart_rate",
        "activity-level": "activity",
        "active-energy-burned": "active_energy",
        "total-calories": "calories",
    }.get(data_type, data_type.replace("-", "_"))
