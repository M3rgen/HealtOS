from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.models.state import JobStatus, RawHealthData, SyncState, WebhookNotification
from app.services import pipeline_health
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


def _raw(identifier, data_type, start, end=None, *, updated=None):
    stamp = updated or start
    return RawHealthData(
        google_data_point_id=identifier,
        data_type=data_type,
        source_start_time=start,
        source_end_time=end,
        raw_json={},
        payload_hash="test",
        first_seen_at=stamp,
        last_seen_at=stamp,
        updated_at=stamp,
    )


async def test_data_freshness_uses_end_time_when_present(sessions):
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)
    await _sync_rows(sessions, now - timedelta(minutes=10))
    async with sessions.begin() as session:
        session.add_all(
            [
                _raw("s1", "steps", now - timedelta(hours=3), now - timedelta(hours=2)),
                _raw("s2", "steps", now - timedelta(hours=1)),
                _raw("h1", "heart-rate", now - timedelta(hours=5), now - timedelta(minutes=30)),
                _raw("h2", "heart-rate", now - timedelta(hours=1)),
                _raw("x1", "sleep", now - timedelta(minutes=5)),
            ]
        )

    snapshot = await _service(sessions).inspect(now=now)

    assert snapshot.data_freshness["steps"] == now - timedelta(hours=1)
    assert snapshot.data_freshness["heart-rate"] == now - timedelta(minutes=30)
    assert "sleep" not in snapshot.data_freshness
    assert "distance" not in snapshot.data_freshness
    assert snapshot.data_freshness_checked_at == now
    assert snapshot.as_dict()["data_freshness"]["heart_rate"] == "2026-08-19T11:30:00Z"


async def test_freshness_summary_is_cached_until_refresh(sessions, monkeypatch):
    clock = {"value": 1_000.0}
    monkeypatch.setattr(pipeline_health.time, "monotonic", lambda: clock["value"])
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)
    await _sync_rows(sessions, now - timedelta(minutes=10))
    async with sessions.begin() as session:
        session.add(_raw("s1", "steps", now - timedelta(hours=2)))
    service = _service(sessions)

    first = await service.inspect(now=now)
    async with sessions.begin() as session:
        session.add(_raw("s2", "steps", now - timedelta(minutes=1)))
    clock["value"] += 3_599
    cached = await service.inspect(now=now + timedelta(minutes=59))

    assert first.data_freshness["steps"] == now - timedelta(hours=2)
    assert cached.data_freshness["steps"] == now - timedelta(hours=2)
    assert cached.data_freshness_checked_at == now

    clock["value"] += 1
    expired = await service.inspect(now=now + timedelta(hours=1))
    assert expired.data_freshness["steps"] == now - timedelta(minutes=1)
    assert expired.data_freshness_checked_at == now + timedelta(hours=1)


async def test_hourly_refresh_updates_cached_summary(sessions):
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)
    await _sync_rows(sessions, now - timedelta(minutes=10))
    service = _service(sessions)
    await service.inspect(now=now)
    assert service.cached_freshness.data_freshness == {}

    async with sessions.begin() as session:
        session.add(_raw("d1", "distance", now - timedelta(minutes=20)))
    summary = await service.refresh_freshness(now=now + timedelta(hours=1))
    snapshot = await service.inspect(now=now + timedelta(hours=1))

    assert summary.data_freshness == {"distance": now - timedelta(minutes=20)}
    assert snapshot.data_freshness == {"distance": now - timedelta(minutes=20)}
    assert snapshot.latest_health_write == now - timedelta(minutes=20)


async def test_sync_state_staleness_is_not_cached(sessions):
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)
    await _sync_rows(sessions, now - timedelta(minutes=10))
    service = _service(sessions)
    assert (await service.inspect(now=now)).status == PipelineStatus.healthy

    snapshot = await service.inspect(now=now + timedelta(hours=3))

    assert snapshot.status == PipelineStatus.stale
