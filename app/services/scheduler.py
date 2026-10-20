from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import Settings
from app.services.pipeline_health import PipelineHealthService
from app.services.watchdog import PipelineWatchdog
from app.sync.reconciliation import ReconciliationService

logger = logging.getLogger(__name__)


def build_scheduler(
    settings: Settings,
    reconciliation: ReconciliationService,
    watchdog: PipelineWatchdog | None = None,
    pipeline_health: PipelineHealthService | None = None,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    if settings.reconciliation_enabled:
        scheduler.add_job(
            reconciliation.run,
            "cron",
            hour=settings.reconciliation_daily_hour_utc,
            kwargs={"days": settings.reconciliation_daily_days},
            id="daily-reconciliation",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            reconciliation.run,
            "cron",
            day_of_week=settings.reconciliation_weekly_day,
            hour=settings.reconciliation_weekly_hour_utc,
            kwargs={"days": settings.reconciliation_weekly_days},
            id="weekly-reconciliation",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    if settings.watchdog_enabled and watchdog is not None:
        scheduler.add_job(
            watchdog.periodic_check,
            "interval",
            seconds=settings.watchdog_interval_seconds,
            id="pipeline-watchdog",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    if pipeline_health is not None:
        # Hourly slot: the only place (besides an expired-cache fallback) that reads
        # raw_health_data for the /health freshness summary.
        scheduler.add_job(
            pipeline_health.refresh_freshness,
            "cron",
            minute=0,
            id="pipeline-freshness-refresh",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    return scheduler
