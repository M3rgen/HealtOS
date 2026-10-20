from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.timeutil import as_utc
from app.google_health.client import Page
from app.google_health.data_types import REGISTRY, query_iso
from app.models.state import JobStatus, RawHealthData, SyncState, WebhookNotification
from app.services.dashboard import dashboard_data
from app.services.webhook_processing import WebhookBatchProcessor
from app.sync.deletions import deletion_scope
from app.sync.incremental import IncrementalSync

WINDOW_START = datetime(2026, 8, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 2, tzinfo=UTC)


def steps(name, hour, count=100):
    return {
        "name": f"users/1/dataTypes/steps/dataPoints/{name}",
        "steps": {
            "interval": {
                "startTime": f"2026-08-01T{hour:02d}:00:00Z",
                "endTime": f"2026-08-01T{hour:02d}:01:00Z",
            },
            "count": str(count),
        },
    }


class QueueClient:
    def __init__(self):
        self.responses = []

    async def list_data_points(self, spec, start, end, page_token=None):
        return self.responses.pop(0)


async def snapshot(sessions):
    async with sessions() as session:
        rows = (await session.scalars(select(RawHealthData))).all()
        dashboard = await dashboard_data(session, date(2026, 8, 1), 1, "UTC")
        with_deleted = await dashboard_data(
            session, date(2026, 8, 1), 1, "UTC", include_source_deleted=True
        )
    deleted = {row.google_data_point_id.rsplit("/", 1)[1] for row in rows if row.source_deleted_at}
    return len(rows), deleted, dashboard["days"][0]["steps"], with_deleted["days"][0]["steps"]


async def test_refetched_window_marks_missing_record_and_restores_it(sessions):
    client = QueueClient()
    service = IncrementalSync(Settings(), sessions, client, {"steps": REGISTRY["steps"]})
    a, b, c = steps("a", 10), steps("b", 11), steps("c", 12)

    client.responses = [Page([a, b, c], None)]
    await service.run("steps", WINDOW_START, WINDOW_END)
    assert await snapshot(sessions) == (3, set(), 300, 300)

    client.responses = [Page([a], "p2"), Page([b], None)]
    stats = await service.run("steps", WINDOW_START, WINDOW_END)
    assert stats.source_deleted == 1
    # Row kept, only marked; the dashboard counts the two live records.
    assert await snapshot(sessions) == (3, {"c"}, 200, 300)

    client.responses = [Page([a, b, c], None)]
    await service.run("steps", WINDOW_START, WINDOW_END)
    assert await snapshot(sessions) == (3, set(), 300, 300)


async def test_records_outside_the_window_are_not_marked(sessions):
    client = QueueClient()
    service = IncrementalSync(Settings(), sessions, client, {"steps": REGISTRY["steps"]})
    client.responses = [Page([steps("a", 10), steps("b", 20)], None)]
    await service.run("steps", WINDOW_START, WINDOW_END)

    client.responses = [Page([steps("a", 10)], None)]
    await service.run("steps", WINDOW_START, WINDOW_START + timedelta(hours=12))

    count, deleted, *_ = await snapshot(sessions)
    assert (count, deleted) == (2, set())


async def test_resumed_window_cannot_infer_deletions(sessions):
    """Pages before a saved token were seen by an earlier process, not by this fetch."""
    client = QueueClient()
    service = IncrementalSync(Settings(), sessions, client, {"steps": REGISTRY["steps"]})
    client.responses = [Page([steps("a", 10), steps("b", 11)], None)]
    await service.run("steps", WINDOW_START, WINDOW_END)
    async with sessions.begin() as session:
        state = await session.get(SyncState, "steps")
        state.status = JobStatus.failed
        state.last_page_token = "page-2"
    later = datetime(2026, 8, 10, tzinfo=UTC)

    client.responses = [Page([], None), Page([], None)]
    stats = await service.run("steps", later, later + timedelta(days=1))

    assert stats.source_deleted == 0
    _, deleted, *_ = await snapshot(sessions)
    assert deleted == set()


def test_deletion_scope_mirrors_the_list_filter_per_record_kind():
    start = datetime(2026, 8, 1, 6, tzinfo=UTC)
    end = datetime(2026, 8, 3, 6, tzinfo=UTC)

    column, lower, upper = deletion_scope(REGISTRY["heart-rate"], start, end)
    assert (column.key, lower, upper) == ("source_start_time", start, end)
    column, lower, upper = deletion_scope(REGISTRY["sleep"], start, end)
    assert (column.key, lower, upper) == ("source_end_time", start, end)
    # Civil-time session filters: keep only records certainly inside for any UTC offset.
    column, lower, upper = deletion_scope(REGISTRY["exercise"], start, end)
    assert (lower, upper) == (start + timedelta(hours=12), end - timedelta(hours=14))
    assert deletion_scope(REGISTRY["exercise"], start, start + timedelta(hours=20)) is None
    # Daily summaries: civil dates, partial end date rounded up like filter_for.
    column, lower, upper = deletion_scope(REGISTRY["daily-resting-heart-rate"], start, end)
    assert (lower, upper) == (datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 4, tzinfo=UTC))
    # Aggregates and catalogs carry no deletable source identity.
    assert deletion_scope(REGISTRY["total-calories"], start, end) is None
    assert deletion_scope(REGISTRY["floors"], start, end) is None
    assert deletion_scope(REGISTRY["food"], start, end) is None


async def test_rollup_refetch_never_marks(sessions):
    client = QueueClient()
    spec = REGISTRY["total-calories"]
    service = IncrementalSync(Settings(), sessions, client, {spec.name: spec})
    point = {
        "startTime": "2026-08-01T10:00:00Z",
        "endTime": "2026-08-01T10:01:00Z",
        "totalCalories": {"kcalSum": 1},
    }
    client.responses = [Page([point], None)]
    await service.run(spec.name, WINDOW_START, WINDOW_END)
    client.responses = [Page([], None)]
    await service.run(spec.name, WINDOW_START, WINDOW_END)
    async with sessions() as session:
        marked = await session.scalar(
            select(func.count()).where(RawHealthData.source_deleted_at.is_not(None))
        )
    assert marked == 0


def steps_at(name, start_iso, count=100):
    point = steps(name, 0, count)
    point["steps"]["interval"] = {"startTime": start_iso, "endTime": start_iso}
    return point


async def test_sub_second_window_end_marks_only_what_google_was_asked_for(sessions):
    """Google receives whole seconds; marking must not reach into the dropped fraction."""
    client = QueueClient()
    service = IncrementalSync(Settings(), sessions, client, {"steps": REGISTRY["steps"]})
    a = steps_at("a", "2026-08-01T10:00:00Z")
    # Inside [00:00:00, 00:00:00.5): the query below asks Google for `< 00:00:00Z` only.
    edge = steps_at("edge", "2026-08-02T00:00:00.200Z")
    client.responses = [Page([a, edge], None)]
    await service.run("steps", WINDOW_START, WINDOW_END + timedelta(seconds=1))

    client.responses = [Page([a], None)]
    stats = await service.run("steps", WINDOW_START, WINDOW_END + timedelta(milliseconds=500))

    assert stats.source_deleted == 0
    _, deleted, *_ = await snapshot(sessions)
    assert deleted == set()


def test_deletion_scope_uses_the_bounds_sent_to_google():
    start = datetime(2026, 8, 1, 6, 0, 0, 700_000, tzinfo=UTC)
    end = datetime(2026, 8, 1, 10, 0, 0, 500_000, tzinfo=UTC)
    _, lower, upper = deletion_scope(REGISTRY["heart-rate"], start, end)
    assert (lower, upper) == (start.replace(microsecond=0), end.replace(microsecond=0))
    assert REGISTRY["heart-rate"].filter_for(query_iso(start), query_iso(end)) == (
        'heart_rate.sample_time.physical_time >= "2026-08-01T06:00:00Z" AND '
        'heart_rate.sample_time.physical_time < "2026-08-01T10:00:00Z"'
    )


def session_record(data_type, name, start_iso, end_iso):
    return {
        "name": f"users/1/dataTypes/{data_type}/dataPoints/{name}",
        data_type: {
            "interval": {"startTime": start_iso, "endTime": end_iso, "startUtcOffset": "10800s"}
        },
    }


class RecordingClient(QueueClient):
    def __init__(self):
        super().__init__()
        self.windows = []

    async def list_data_points(self, spec, start, end, page_token=None):
        self.windows.append((start, end))
        return await super().list_data_points(spec, start, end, page_token)


async def webhook_delete(sessions, service, data_type, start_iso, end_iso):
    processor = WebhookBatchProcessor(sessions, service, set(service.specs))
    async with sessions.begin() as session:
        session.add(
            WebhookNotification(
                element_hash=f"delete-{data_type}",
                raw_json={},
                is_valid=True,
                data_type=data_type,
                health_user_id="user-1",
                operation="DELETE",
                intervals=[{"physicalTimeInterval": {"startTime": start_iso, "endTime": end_iso}}],
            )
        )
    assert await processor._process_element(f"delete-{data_type}") == "completed"


@pytest.mark.parametrize(
    "data_type, start_iso, end_iso",
    [
        # The filter is end_time < end: a session ending exactly at the notified end.
        ("sleep", "2026-08-01T22:00:00Z", "2026-08-02T06:00:00Z"),
        # The filter is on civil start time: a short UTC+3 workout.
        ("exercise", "2026-08-01T10:00:00Z", "2026-08-01T11:00:00Z"),
    ],
)
async def test_webhook_delete_of_one_session_marks_it(sessions, data_type, start_iso, end_iso):
    client = RecordingClient()
    service = IncrementalSync(Settings(), sessions, client, {data_type: REGISTRY[data_type]})
    record = session_record(data_type, "gone", start_iso, end_iso)
    client.responses = [Page([record], None)]
    await service.run(data_type, WINDOW_START, WINDOW_START + timedelta(days=3))

    client.responses = [Page([], None)]
    await webhook_delete(sessions, service, data_type, start_iso, end_iso)

    async with sessions() as session:
        row = await session.scalar(select(RawHealthData))
        state = await session.get(SyncState, data_type)
    assert row.source_deleted_at is not None
    # The query sent to Google covered the session for any UTC offset, so the mark is safe.
    start, end = client.windows[-1]
    begin, finish = datetime.fromisoformat(start_iso), datetime.fromisoformat(end_iso)
    if data_type == "sleep":
        assert start <= finish < end
    else:
        assert start <= begin - timedelta(hours=12) and end >= begin + timedelta(hours=14)
    assert as_utc(state.last_successful_sync) <= datetime.now(UTC)


async def test_widened_webhook_window_never_moves_the_watermark_past_now(sessions):
    client = RecordingClient()
    service = IncrementalSync(Settings(), sessions, client, {"exercise": REGISTRY["exercise"]})
    begin = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    client.responses = [Page([], None)]
    await webhook_delete(
        sessions,
        service,
        "exercise",
        begin.isoformat().replace("+00:00", "Z"),
        (begin + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
    )
    async with sessions() as session:
        state = await session.get(SyncState, "exercise")
    assert client.windows[-1][1] > datetime.now(UTC)  # civil start times run ahead of UTC
    assert as_utc(state.last_successful_sync) <= datetime.now(UTC)
