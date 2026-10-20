from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select

from app.core.config import Settings
from app.google_health.client import Page
from app.google_health.data_types import REGISTRY
from app.models.state import JobStatus, RawHealthData, SyncState
from app.services.dashboard import dashboard_data
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
