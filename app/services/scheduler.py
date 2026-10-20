from __future__ import annotations

import logging

from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import Settings
from app.core.logging import event
from app.services.pipeline_health import PipelineHealthService
from app.services.watchdog import PipelineWatchdog
from app.sync.reconciliation import ReconciliationService

logger = logging.getLogger(__name__)

# A blocked event loop (e.g. hashing a large record batch) must not silently drop the
# daily reconciliation: run a late job within an hour, and collapse missed runs to one.
JOB_DEFAULTS = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 3600}


def log_missed_job(scheduled: JobExecutionEvent) -> None:
    event(
        logger,
        "SCHEDULER_JOB_MISSED",
        job_id=scheduled.job_id,
        scheduled_run_time=str(scheduled.scheduled_run_time),
    )


def build_scheduler(
    settings: Settings,
    reconciliation: ReconciliationService,
    watchdog: PipelineWatchdog | None = None,
    pipeline_health: PipelineHealthService | None = None,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_listener(log_missed_job, EVENT_JOB_MISSED)
    if settings.reconciliation_enabled:
        scheduler.add_job(
            reconciliation.run,
            "cron",
            hour=settings.reconciliation_daily_hour_utc,
            kwargs={"days": settings.reconciliation_daily_days},
            id="daily-reconciliation",
            replace_existing=True,
            **JOB_DEFAULTS,
        )
        scheduler.add_job(
            reconciliation.run,
            "cron",
            day_of_week=settings.reconciliation_weekly_day,
            hour=settings.reconciliation_weekly_hour_utc,
            kwargs={"days": settings.reconciliation_weekly_days},
            id="weekly-reconciliation",
            replace_existing=True,
            **JOB_DEFAULTS,
        )
    if settings.watchdog_enabled and watchdog is not None:
        scheduler.add_job(
            watchdog.periodic_check,
            "interval",
            seconds=settings.watchdog_interval_seconds,
            id="pipeline-watchdog",
            replace_existing=True,
            **JOB_DEFAULTS,
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
            **JOB_DEFAULTS,
        )
    return scheduler
