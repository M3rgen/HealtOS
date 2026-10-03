from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select

from app.core.config import Settings
from app.google_health.client import Page
from app.google_health.data_types import REGISTRY
from app.models.health import Steps
from app.models.state import BackfillJob, JobStatus, RawHealthData
from app.sync.backfill import BackfillService
from app.sync.incremental import IncrementalSync


def step(count):
    return {
        "name": "users/1/dataTypes/steps/dataPoints/workflow",
        "steps": {
            "interval": {
                "startTime": "2026-08-17T10:00:00Z",
                "endTime": "2026-08-17T10:01:00Z",
            },
            "count": str(count),
        },
    }


class QueueClient:
    def __init__(self, pages):
        self.pages = list(pages)

    async def list_data_points(self, spec, start, end, page_token=None):
        return self.pages.pop(0)


async def test_mock_backfill_then_incremental_correction(sessions):
    settings = Settings(backfill_start_date=date.today() - timedelta(days=1))
    backfill = BackfillService(
        settings,
        sessions,
        QueueClient([Page([step(40)], None)]),
        {"steps": REGISTRY["steps"]},
    )
    assert await backfill.run(["steps"]) == 1
    incremental = IncrementalSync(
        settings,
        sessions,
        QueueClient([Page([step(45)], None)]),
        {"steps": REGISTRY["steps"]},
    )
    await incremental.run("steps")
    async with sessions() as session:
        assert await session.scalar(select(func.count(RawHealthData.id))) == 1
        assert await session.scalar(select(func.count(Steps.id))) == 1
        normalized = await session.scalar(select(Steps))
        jobs = (await session.scalars(select(BackfillJob))).all()
    assert int(normalized.count) == 45
    assert all(job.status == JobStatus.completed for job in jobs)


async def test_mock_full_backfill_traverses_every_window_and_page(sessions):
    class WindowedClient:
        def __init__(self):
            self.calls = []

        async def list_data_points(self, spec, start, end, page_token=None):
            self.calls.append((start, end, page_token))
            page = "second" if page_token else "first"
            record = {
                "name": f"users/1/dataTypes/steps/dataPoints/{start.date()}-{page}",
                "steps": {
                    "interval": {
                        "startTime": start.isoformat().replace("+00:00", "Z"),
                        "endTime": min(end, start + timedelta(minutes=1))
                        .isoformat()
                        .replace("+00:00", "Z"),
                    },
                    "count": "1",
                },
            }
            return Page([record], None if page_token else "second-page")

    client = WindowedClient()
    service = BackfillService(
        Settings(
            backfill_start_date=datetime.now(UTC).date() - timedelta(days=2),
            backfill_window_days=1,
        ),
        sessions,
        client,
        {"steps": REGISTRY["steps"]},
    )

    completed = await service.run(["steps"])

    async with sessions() as session:
        jobs = (await session.scalars(select(BackfillJob))).all()
        raw_count = await session.scalar(select(func.count(RawHealthData.id)))
    assert completed == len(jobs)
    assert len(jobs) >= 2
    assert all(job.status == JobStatus.completed and job.records_downloaded == 2 for job in jobs)
    assert [token for _, _, token in client.calls] == [None, "second-page"] * len(jobs)
    assert raw_count == 2 * len(jobs)
