from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.config import Settings
from app.google_health.client import Page
from app.google_health.data_types import REGISTRY
from app.models.state import BackfillJob, JobStatus, SyncState
from app.sync.backfill import BackfillService
from app.sync.incremental import IncrementalSync


class FakeClient:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.tokens = []

    async def list_data_points(self, spec, start, end, page_token=None):
        self.tokens.append(page_token)
        return next(self.pages)


async def test_incremental_sync_commits_checkpoint(sessions):
    client = FakeClient([Page([], "p2"), Page([], None)])
    service = IncrementalSync(Settings(), sessions, client, {"steps": REGISTRY["steps"]})
    await service.run("steps")
    async with sessions() as session:
        state = await session.get(SyncState, "steps")
    assert client.tokens == [None, "p2"]
    assert state.status == JobStatus.completed
    assert state.last_page_token is None
    assert state.last_successful_sync is not None


async def test_targeted_historical_sync_does_not_regress_checkpoint(sessions):
    current = datetime(2026, 8, 18, tzinfo=UTC)
    async with sessions.begin() as session:
        session.add(
            SyncState(
                data_type="steps",
                status=JobStatus.completed,
                last_successful_sync=current,
            )
        )
    service = IncrementalSync(
        Settings(), sessions, FakeClient([Page([], None)]), {"steps": REGISTRY["steps"]}
    )

    await service.run(
        "steps",
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
    )

    async with sessions() as session:
        state = await session.get(SyncState, "steps")
    assert state.last_successful_sync.replace(tzinfo=UTC) == current


async def test_backfill_resumes_saved_page_token(sessions):
    start = datetime.now(UTC) - timedelta(days=1)
    end = datetime.now(UTC) + timedelta(days=1)
    async with sessions.begin() as session:
        session.add(
            BackfillJob(
                data_type="steps",
                window_start=start,
                window_end=end,
                status=JobStatus.running,
                page_token="resume-me",
            )
        )
    client = FakeClient([Page([], None)])
    settings = Settings(backfill_start_date=end.date(), backfill_window_days=31)
    service = BackfillService(settings, sessions, client, {"steps": REGISTRY["steps"]})
    assert await service.run(["steps"]) == 1
    assert client.tokens == ["resume-me"]
    async with sessions() as session:
        job = await session.scalar(select(BackfillJob))
    assert job.status == JobStatus.completed


async def test_backfill_failure_does_not_block_other_data_types(sessions):
    class IsolatedClient:
        async def list_data_points(self, spec, start, end, page_token=None):
            if spec.name == "steps":
                raise RuntimeError("unavailable scope")
            return Page([], None)

    settings = Settings(backfill_start_date=datetime.now(UTC).date())
    service = BackfillService(
        settings,
        sessions,
        IsolatedClient(),
        {"steps": REGISTRY["steps"], "sleep": REGISTRY["sleep"]},
    )
    assert await service.run() == 1
    async with sessions() as session:
        jobs = (await session.scalars(select(BackfillJob))).all()
    statuses = {job.data_type: job.status for job in jobs}
    assert statuses == {"steps": JobStatus.failed, "sleep": JobStatus.completed}


async def test_unfiltered_reference_backfill_creates_only_one_snapshot_job(sessions):
    settings = Settings(backfill_start_date=datetime.now(UTC).date())
    service = BackfillService(
        settings,
        sessions,
        FakeClient([Page([], None)]),
        {"food": REGISTRY["food"]},
    )

    assert await service.ensure_jobs(["food"]) == 1
    assert await service.ensure_jobs(["food"]) == 0
