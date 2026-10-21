import logging
from datetime import UTC, datetime

from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent

from app.core.config import Settings
from app.services.scheduler import build_scheduler, log_missed_job


def test_daily_and_weekly_reconciliation_schedule_is_configurable():
    class Reconciliation:
        async def run(self, days):
            return days

    scheduler = build_scheduler(
        Settings(
            reconciliation_enabled=True,
            reconciliation_daily_hour_utc=4,
            reconciliation_daily_days=8,
            reconciliation_weekly_day="mon",
            reconciliation_weekly_hour_utc=5,
            reconciliation_weekly_days=91,
        ),
        Reconciliation(),
    )

    daily = scheduler.get_job("daily-reconciliation")
    weekly = scheduler.get_job("weekly-reconciliation")
    assert daily.kwargs == {"days": 8}
    assert weekly.kwargs == {"days": 91}
    assert "hour='4'" in str(daily.trigger)
    assert "day_of_week='mon'" in str(weekly.trigger)
    assert "hour='5'" in str(weekly.trigger)


def test_watchdog_schedule_is_bounded_and_non_overlapping():
    class Reconciliation:
        async def run(self, days):
            return days

    class Watchdog:
        async def periodic_check(self):
            return None

    scheduler = build_scheduler(
        Settings(watchdog_enabled=True, watchdog_interval_seconds=600),
        Reconciliation(),
        Watchdog(),
    )
    job = scheduler.get_job("pipeline-watchdog")

    assert job.max_instances == 1
    assert "0:10:00" in str(job.trigger)


def test_freshness_summary_refreshes_in_hourly_slots():
    class Reconciliation:
        async def run(self, days):
            return days

    class Health:
        async def refresh_freshness(self):
            return None

    scheduler = build_scheduler(
        Settings(reconciliation_enabled=False, watchdog_enabled=True),
        Reconciliation(),
        None,
        Health(),
    )
    job = scheduler.get_job("pipeline-freshness-refresh")

    assert job.max_instances == 1
    assert "minute='0'" in str(job.trigger)


def test_every_job_tolerates_late_starts_and_coalesces_missed_runs():
    class Reconciliation:
        async def run(self, days):
            return days

    class Watchdog:
        async def periodic_check(self):
            return None

    class Health:
        async def refresh_freshness(self):
            return None

    class Identity:
        async def run(self):
            return None

    scheduler = build_scheduler(
        Settings(reconciliation_enabled=True, watchdog_enabled=True),
        Reconciliation(),
        Watchdog(),
        Health(),
        Identity(),
    )
    jobs = scheduler.get_jobs()

    assert {job.id for job in jobs} == {
        "daily-reconciliation",
        "weekly-reconciliation",
        "pipeline-watchdog",
        "pipeline-freshness-refresh",
        "identity-reconcile",
    }
    for job in jobs:
        assert job.misfire_grace_time == 3600
        assert job.coalesce is True
        assert job.max_instances == 1
    assert any(
        callback is log_missed_job and mask & EVENT_JOB_MISSED
        for callback, mask in scheduler._listeners
    )


def test_missed_job_is_logged_at_info(caplog, monkeypatch):
    # Alembic's fileConfig in the migration tests disables already-created loggers.
    monkeypatch.setattr(logging.getLogger("app.services.scheduler"), "disabled", False)
    with caplog.at_level(logging.INFO, logger="app.services.scheduler"):
        log_missed_job(
            JobExecutionEvent(
                EVENT_JOB_MISSED,
                "daily-reconciliation",
                "default",
                datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
    [record] = [item for item in caplog.records if item.message == "SCHEDULER_JOB_MISSED"]
    assert record.levelno == logging.INFO
    assert record.job_id == "daily-reconciliation"


def test_identity_reconciler_runs_nightly_at_a_configurable_quiet_time():
    class Reconciliation:
        async def run(self, days):
            return days

    class Identity:
        async def run(self):
            return None

    default = build_scheduler(Settings(), Reconciliation(), identity=Identity())
    job = default.get_job("identity-reconcile")
    # 02:30 UTC: after the daily Google reconciliation starts, away from the 03:30 backup.
    assert "hour='2'" in str(job.trigger) and "minute='30'" in str(job.trigger)
    moved = build_scheduler(
        Settings(identity_reconcile_hour_utc=4, identity_reconcile_minute=15),
        Reconciliation(),
        identity=Identity(),
    )
    assert "hour='4', minute='15'" in str(moved.get_job("identity-reconcile").trigger)
    off = build_scheduler(
        Settings(identity_reconcile_enabled=False), Reconciliation(), identity=Identity()
    )
    assert off.get_job("identity-reconcile") is None
