from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.models.state import JobStatus, ReliabilityState, SyncState
from app.services.pipeline_health import PipelineSnapshot, PipelineStatus
from app.services.watchdog import PipelineWatchdog


class Health:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    async def inspect(self, **kwargs):
        return self.snapshot


class Reconciliation:
    running = False

    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else {"steps": object()}

    async def run(self, days):
        self.calls.append(days)
        return self.result


def _snapshot(now, status=PipelineStatus.stale, age=timedelta(hours=3)):
    return PipelineSnapshot(
        status=status,
        checked_at=now,
        last_webhook_received=None,
        last_webhook_processed=None,
        last_sync_success=now - age,
        latest_frequent_sync=now - age,
        latest_health_write=None,
        last_reconciliation=None,
        pending_notifications=0,
        failed_notifications=0,
        failed_syncs=0,
    )


def _watchdog(sessions, now, reconciliation=None, **settings):
    config = Settings(
        _env_file=None,
        startup_gap_threshold_minutes=120,
        recovery_cooldown_minutes=360,
        **settings,
    )
    reconciliation = reconciliation or Reconciliation()
    return (
        PipelineWatchdog(config, sessions, Health(_snapshot(now)), reconciliation),
        reconciliation,
    )


async def test_startup_gap_triggers_minimum_reconciliation(sessions):
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)
    watchdog, reconciliation = _watchdog(sessions, now)

    result = await watchdog.check(trigger="startup", now=now)

    assert result.action == "completed"
    assert result.days == 1
    assert reconciliation.calls == [1]


async def test_watchdog_cooldown_prevents_recovery_loop(sessions):
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)
    async with sessions.begin() as session:
        session.add(ReliabilityState(id=1, last_recovery_attempt_at=now - timedelta(minutes=5)))
    watchdog, reconciliation = _watchdog(sessions, now)

    result = await watchdog.check(trigger="watchdog", now=now)

    assert result.action == "cooldown"
    assert reconciliation.calls == []


async def test_watchdog_prevents_overlapping_recovery(sessions):
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)
    watchdog, reconciliation = _watchdog(sessions, now)
    await watchdog._recovery_lock.acquire()
    try:
        result = await watchdog.periodic_check()
    finally:
        watchdog._recovery_lock.release()

    assert result.action == "overlap"
    assert reconciliation.calls == []


async def test_recovery_failure_is_persisted(sessions):
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)
    reconciliation = Reconciliation({"steps": RuntimeError("upstream unavailable")})
    watchdog, _ = _watchdog(sessions, now, reconciliation)

    result = await watchdog.periodic_check()

    async with sessions() as session:
        state = await session.get(ReliabilityState, 1)
    assert result.action == "failed"
    assert result.error_types == ("RuntimeError",)
    assert state.last_recovery_failure_at is not None
    assert state.last_recovery_error == "RuntimeError"


async def test_fresh_startup_does_not_trigger_recovery(sessions):
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)
    watchdog, reconciliation = _watchdog(sessions, now)
    watchdog.health.snapshot = _snapshot(
        now, status=PipelineStatus.healthy, age=timedelta(minutes=10)
    )

    result = await watchdog.check(trigger="startup", now=now)

    assert result.action == "not_needed"
    assert reconciliation.calls == []


async def test_startup_uses_latest_write_when_sync_state_is_missing(sessions):
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)
    watchdog, reconciliation = _watchdog(sessions, now)
    watchdog.health.snapshot = PipelineSnapshot(
        status=PipelineStatus.degraded,
        checked_at=now,
        last_webhook_received=None,
        last_webhook_processed=None,
        last_sync_success=None,
        latest_frequent_sync=None,
        latest_health_write=now - timedelta(hours=4),
        last_reconciliation=None,
        pending_notifications=0,
        failed_notifications=0,
        failed_syncs=0,
    )

    result = await watchdog.check(trigger="startup", now=now)

    assert result.action == "completed"
    assert reconciliation.calls == [1]


async def test_startup_gap_uses_oldest_frequent_stream(sessions):
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    watchdog, reconciliation = _watchdog(sessions, now)
    watchdog.health.snapshot = PipelineSnapshot(
        status=PipelineStatus.degraded,
        checked_at=now,
        last_webhook_received=now - timedelta(minutes=1),
        last_webhook_processed=now - timedelta(minutes=1),
        last_sync_success=now - timedelta(minutes=1),
        latest_frequent_sync=now - timedelta(minutes=1),
        oldest_frequent_sync=now - timedelta(days=6),
        latest_health_write=now - timedelta(minutes=1),
        last_reconciliation=None,
        pending_notifications=0,
        failed_notifications=0,
        failed_syncs=0,
    )

    result = await watchdog.check(trigger="startup", now=now)

    assert result.action == "completed"
    assert reconciliation.calls == [6]


async def test_only_a_live_running_sync_blocks_recovery(sessions):
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)
    async with sessions.begin() as session:
        # Left behind by a dead process: no heartbeat for an hour.
        session.add(
            SyncState(
                data_type="steps", status=JobStatus.running, updated_at=now - timedelta(hours=1)
            )
        )
    watchdog, reconciliation = _watchdog(sessions, now)

    assert (await watchdog.check(trigger="watchdog", now=now)).action == "completed"

    async with sessions.begin() as session:
        session.add(
            SyncState(
                data_type="sleep", status=JobStatus.running, updated_at=now - timedelta(minutes=1)
            )
        )
        await session.delete(await session.get(ReliabilityState, 1))
    assert (await watchdog.check(trigger="watchdog", now=now)).action == "sync_running"
