from datetime import UTC, datetime, timedelta

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from app.api import backfill as backfill_api
from app.core.config import Settings
from app.db.session import get_session
from app.google_health.client import Page
from app.google_health.data_types import REGISTRY
from app.models.state import BackfillJob, JobStatus
from app.sync.backfill import BackfillService

NOW = datetime.now(UTC)
POISON_START = NOW - timedelta(days=2)
HEALTHY_START = NOW - timedelta(days=4)


class PoisonClient:
    """Rejects one window forever (like a 403 for one range); serves the others."""

    def __init__(self):
        self.starts = []

    async def list_data_points(self, spec, start, end, page_token=None):
        self.starts.append(start.replace(tzinfo=UTC))
        if start.replace(tzinfo=UTC) == POISON_START:
            raise RuntimeError("Google Health API returned HTTP 403: PERMISSION_DENIED")
        return Page([], None)


async def add_windows(sessions, poison_retries):
    async with sessions.begin() as session:
        session.add_all(
            [
                # Newest window first: it used to be picked again on every run.
                BackfillJob(
                    data_type="steps",
                    window_start=POISON_START,
                    window_end=POISON_START + timedelta(days=1),
                    status=JobStatus.failed,
                    retry_count=poison_retries,
                ),
                BackfillJob(
                    data_type="steps",
                    window_start=HEALTHY_START,
                    window_end=HEALTHY_START + timedelta(days=1),
                    status=JobStatus.pending,
                ),
            ]
        )


def service(sessions, client):
    # Backfill boundaries already cover the configured start, so no new windows appear
    # except the trailing range up to now, which the healthy client serves.
    settings = Settings(backfill_start_date=HEALTHY_START.date(), backfill_window_days=31)
    return BackfillService(settings, sessions, client, {"steps": REGISTRY["steps"]})


async def jobs_by_start(sessions):
    async with sessions() as session:
        jobs = (await session.scalars(select(BackfillJob))).all()
    return {job.window_start.replace(tzinfo=UTC): job for job in jobs}


async def test_poison_window_is_deprioritized_and_healthy_window_is_processed(sessions):
    await add_windows(sessions, poison_retries=5)
    client = PoisonClient()

    await service(sessions, client).run(["steps"])

    jobs = await jobs_by_start(sessions)
    assert jobs[HEALTHY_START].status == JobStatus.completed
    assert jobs[POISON_START].status == JobStatus.failed
    assert jobs[POISON_START].retry_count == 6
    assert client.starts.index(HEALTHY_START) < client.starts.index(POISON_START)


async def test_window_is_abandoned_after_ten_failures_and_not_retried(sessions):
    await add_windows(sessions, poison_retries=9)
    client = PoisonClient()
    backfill = service(sessions, client)

    await backfill.run(["steps"])
    jobs = await jobs_by_start(sessions)
    assert jobs[POISON_START].status == JobStatus.abandoned
    assert jobs[POISON_START].retry_count == 10

    client.starts.clear()
    await backfill.run(["steps"])
    assert POISON_START not in client.starts


async def test_backfill_status_lists_abandoned_windows(sessions):
    await add_windows(sessions, poison_retries=9)
    await service(sessions, PoisonClient()).run(["steps"])
    application = FastAPI()
    application.include_router(backfill_api.router)

    async def session_override():
        async with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = session_override
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/backfill/status")

    rows = {row["status"]: row for row in response.json()}
    assert response.status_code == 200
    assert rows["abandoned"]["windows"] == 1
    assert rows["abandoned"]["data_type"] == "steps"
