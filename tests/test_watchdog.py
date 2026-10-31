import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from reconciliation_records import Client, service

from app.core.config import Settings
from app.google_health.client import Page
from app.google_health.oauth import OAuthError
from app.models.reconciliation import ReconciliationResult, ReconciliationRun
from app.models.state import JobStatus, ReliabilityState, SyncState
from app.services import reconciliation_reports as reports
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

    async def run(self, days, *, run_id):
        self.calls.append(days)
        now = datetime.now(UTC)
        await reports.begin_run(
            self.sessions, run_id, now - timedelta(days=days), now, now, self.result
        )
        for data_type, result in self.result.items():
            await reports.start_type(self.sessions, run_id, data_type)
            await reports.finish_type(
                self.sessions, run_id, data_type, result if isinstance(result, Exception) else None
            )
        await reports.finish_run(self.sessions, run_id)
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
    reconciliation.sessions = sessions
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


@pytest.mark.parametrize("status", ["partial", "failed", "cancelled", "running"])
async def test_watchdog_rejects_persisted_unsuccessful_report_despite_success_dictionary(
    sessions, status
):
    class Reported(Reconciliation):
        async def run(self, days, *, run_id):
            await super().run(days, run_id=run_id)
            async with self.sessions.begin() as session:
                run = await session.get(ReconciliationRun, run_id)
                run.status = status
                if status == "running":
                    run.finished_at = None
            return {"steps": object()}  # An in-memory success cannot override the report.

    now = datetime.now(UTC)
    watchdog, _ = _watchdog(sessions, now, Reported())
    result = await watchdog.check(trigger="watchdog", now=now)
    assert result.action == "failed"
    async with sessions() as session:
        state = await session.get(ReliabilityState, 1)
        assert state.last_recovery_success_at is None
        assert state.last_recovery_failure_at is not None


async def test_watchdog_reads_type_results_even_if_run_claims_success(sessions):
    class IncompleteType(Reconciliation):
        async def run(self, days, *, run_id):
            await super().run(days, run_id=run_id)
            async with self.sessions.begin() as session:
                row = await session.get(ReconciliationResult, (run_id, "steps"))
                row.status, row.finished_at = "running", None
            return {}

    watchdog, _ = _watchdog(sessions, datetime.now(UTC), IncompleteType())
    result = await watchdog.periodic_check()
    assert result.action == "failed"
    assert result.error_types == ("IncompleteReconciliationReport",)


async def test_watchdog_uses_committed_report_instead_of_returned_exception_dictionary(sessions):
    class DurableSuccess(Reconciliation):
        async def run(self, days, *, run_id):
            await super().run(days, run_id=run_id)
            return {"steps": RuntimeError("synthetic stale in-memory result")}

    watchdog, _ = _watchdog(sessions, datetime.now(UTC), DurableSuccess())
    assert (await watchdog.periodic_check()).action == "completed"


async def test_watchdog_requires_its_own_report_not_an_unrelated_success(sessions):
    class WrongInvocation(Reconciliation):
        async def run(self, days, *, run_id):
            return await super().run(days, run_id=uuid.uuid4())

    watchdog, _ = _watchdog(sessions, datetime.now(UTC), WrongInvocation())
    result = await watchdog.periodic_check()
    assert result.action == "failed"
    assert result.error_types == ("MissingReconciliationReport",)


async def test_real_reconciliation_partial_failure_is_never_successful_recovery(sessions):
    now = datetime.now(UTC)
    runner = service(sessions, Client(Page([], "next"), RuntimeError("synthetic")))
    watchdog, _ = _watchdog(sessions, now, runner)
    result = await watchdog.check(trigger="watchdog", now=now)
    async with sessions() as session:
        history = await reports.list_runs(session)
        state = await session.get(ReliabilityState, 1)
    assert history["runs"][0]["status"] == "partial"
    assert result.action == "failed" and result.error_types == ("RuntimeError",)
    assert state.last_recovery_success_at is None


async def test_real_reconciliation_recovery_is_capped_and_persists_success(sessions):
    now = datetime.now(UTC)
    runner = service(sessions, Client())
    watchdog, _ = _watchdog(sessions, now, runner, automatic_recovery_max_days=2)
    watchdog.health.snapshot = _snapshot(now, age=timedelta(days=30))
    result = await watchdog.check(trigger="startup", now=now)
    async with sessions() as session:
        run = (await reports.list_runs(session))["runs"][0]
        state = await session.get(ReliabilityState, 1)
    assert result.action == "completed" and result.days == 2
    assert run["requested_to"] - run["requested_from"] == timedelta(days=2)
    assert run["status"] == "completed"
    assert state.last_recovery_success_at is not None
    assert (await watchdog.check(trigger="watchdog", now=now)).action == "cooldown"


async def test_reconciliation_lock_and_global_lock_still_prevent_recovery(sessions, monkeypatch):
    watchdog, runner = _watchdog(sessions, datetime.now(UTC))
    runner.running = True
    assert (await watchdog.periodic_check()).action == "overlap"
    runner.running = False

    @asynccontextmanager
    async def locked():
        yield False

    monkeypatch.setattr(watchdog, "_global_lock", locked)
    assert (await watchdog.periodic_check()).action == "overlap"
    assert runner.calls == []


async def test_disabled_recovery_creates_no_report(sessions):
    watchdog, runner = _watchdog(sessions, datetime.now(UTC), automatic_recovery_enabled=False)
    assert (await watchdog.periodic_check()).action == "disabled"
    assert runner.calls == []
    async with sessions() as session:
        assert (await reports.list_runs(session))["total"] == 0


@pytest.mark.parametrize("transient", [False, True])
async def test_warmup_failure_stops_recovery_before_fetching_any_type(sessions, transient):
    now = datetime.now(UTC)
    client = Client()
    client.warm_up = AsyncMock(side_effect=OAuthError("synthetic", transient=transient))
    runner = service(sessions, client)
    watchdog, _ = _watchdog(sessions, now, runner)
    result = await watchdog.check(trigger="startup", now=now)
    assert result.action == "failed" and result.error_types == ("OAuthError",)
    client.warm_up.assert_awaited_once()
    assert client.tokens == []
    async with sessions() as session:
        state = await session.get(ReliabilityState, 1)
        run = (await reports.list_runs(session))["runs"][0]
        assert await session.get(SyncState, "heart-rate") is None
    assert state.last_recovery_failure_at and state.last_reconciliation_failure_at
    assert state.last_recovery_success_at is None
    assert run["status"] == "failed" and run["error_type"] == "OAuthError"
    assert all(row["status"] == "failed" for row in run["types"])
