from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.models.state import JobStatus, SyncState, WebhookNotification
from app.services.pipeline_health import PipelineHealthService, PipelineStatus

FREQUENT = {
    "heart-rate",
    "activity-level",
    "steps",
    "distance",
    "total-calories",
}


async def _sync_rows(sessions, when, *, include_sleep=False):
    async with sessions.begin() as session:
        for data_type in FREQUENT:
            session.add(
                SyncState(
                    data_type=data_type,
                    status=JobStatus.completed,
                    last_successful_sync=when,
                    records_downloaded=0,
                )
            )
        if include_sleep:
            session.add(
                SyncState(
                    data_type="sleep",
                    status=JobStatus.completed,
                    last_successful_sync=when - timedelta(days=2),
                    records_downloaded=0,
                )
            )


def _service(sessions):
    return PipelineHealthService(
        Settings(_env_file=None, pipeline_stale_threshold_minutes=120),
        sessions,
        FREQUENT,
    )


async def test_healthy_pipeline_detection(sessions):
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)
    await _sync_rows(sessions, now - timedelta(minutes=10))

    snapshot = await _service(sessions).inspect(now=now)

    assert snapshot.status == PipelineStatus.healthy
    assert snapshot.stale_streams == ()


async def test_stale_pipeline_detection(sessions):
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)
    await _sync_rows(sessions, now - timedelta(hours=3))

    snapshot = await _service(sessions).inspect(now=now)

    assert snapshot.status == PipelineStatus.stale
    assert set(snapshot.stale_streams) == FREQUENT


async def test_low_frequency_dataset_does_not_cause_false_stale_status(sessions):
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)
    await _sync_rows(sessions, now - timedelta(minutes=10), include_sleep=True)

    snapshot = await _service(sessions).inspect(now=now)

    assert snapshot.status == PipelineStatus.healthy
    assert "sleep" not in snapshot.stale_streams


async def test_webhook_backlog_degrades_status(sessions):
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)
    await _sync_rows(sessions, now - timedelta(minutes=10))
    async with sessions.begin() as session:
        session.add(
            WebhookNotification(
                element_hash="a" * 64,
                raw_json={},
                is_valid=True,
                status="pending",
                attempt_count=0,
                sync_attempt_count=0,
                sync_success_count=0,
            )
        )

    snapshot = await _service(sessions).inspect(now=now)

    assert snapshot.status == PipelineStatus.degraded
    assert snapshot.pending_notifications == 1
    assert "webhook_backlog" in snapshot.reasons


async def test_failed_sync_marks_pipeline_failed(sessions):
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)
    await _sync_rows(sessions, now - timedelta(minutes=10))
    async with sessions.begin() as session:
        row = await session.get(SyncState, "steps")
        row.status = JobStatus.failed
        row.last_error = "upstream rejected request"

    snapshot = await _service(sessions).inspect(now=now)

    assert snapshot.status == PipelineStatus.failed
    assert snapshot.failed_syncs == 1
