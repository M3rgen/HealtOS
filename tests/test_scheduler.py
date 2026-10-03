from app.core.config import Settings
from app.services.scheduler import build_scheduler


def test_daily_and_weekly_reconciliation_schedule_is_configurable():
    class Reconciliation:
        async def run(self, days):
            return days

    scheduler = build_scheduler(
        Settings(
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
        Settings(watchdog_interval_seconds=600),
        Reconciliation(),
        Watchdog(),
    )
    job = scheduler.get_job("pipeline-watchdog")

    assert job.max_instances == 1
    assert "0:10:00" in str(job.trigger)
