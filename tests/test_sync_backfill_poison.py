import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from app.api import backfill as backfill_api
from app.cli import main as cli
from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_session
from app.google_health.client import GoogleHealthError, Page
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
            raise GoogleHealthError("Google Health API returned HTTP 403: PERMISSION_DENIED")
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


async def jobs_by_start(sessions, data_type="steps"):
    async with sessions() as session:
        jobs = (
            await session.scalars(select(BackfillJob).where(BackfillJob.data_type == data_type))
        ).all()
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
    # Short class name + code; upstream response text is not exposed.
    assert rows["abandoned"]["last_error"] == "GoogleHealthError (http_403)"
    assert rows["completed"]["last_error"] is None


async def test_reset_abandoned_requeues_only_the_selected_type(sessions):
    await add_windows(sessions, poison_retries=9)
    backfill = service(sessions, PoisonClient())
    await backfill.run(["steps"])
    async with sessions.begin() as session:
        session.add(
            BackfillJob(
                data_type="sleep",
                window_start=POISON_START,
                window_end=POISON_START + timedelta(days=1),
                status=JobStatus.abandoned,
                retry_count=10,
            )
        )

    assert await backfill.reset_abandoned(["steps"]) == 1

    jobs = await jobs_by_start(sessions)
    sleep = (await jobs_by_start(sessions, "sleep"))[POISON_START]
    assert (jobs[POISON_START].status, jobs[POISON_START].retry_count) == (JobStatus.failed, 0)
    # The cause stays visible until the next attempt replaces it.
    assert jobs[POISON_START].last_error == "GoogleHealthError (http_403)"
    assert (sleep.status, sleep.retry_count) == (JobStatus.abandoned, 10)
    assert await backfill.reset_abandoned() == 1


class HealthyClient:
    async def list_data_points(self, spec, start, end, page_token=None):
        return Page([], None)


def test_cli_reset_abandoned_requeues_then_runs(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}", poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await add_windows(sessions, poison_retries=10)
        async with sessions.begin() as session:
            job = await session.scalar(
                select(BackfillJob).where(BackfillJob.window_start == POISON_START)
            )
            job.status = JobStatus.abandoned

    asyncio.run(setup())
    backfill = service(sessions, HealthyClient())
    runtime = SimpleNamespace(backfill=backfill, shutdown=AsyncMock())
    monkeypatch.setattr(cli, "build_runtime", lambda *_: runtime)
    monkeypatch.setattr(cli, "get_settings", lambda: backfill.settings)
    monkeypatch.setattr(cli, "SessionLocal", sessions)

    result = CliRunner().invoke(cli.app, ["backfill", "--reset-abandoned", "--data-type", "steps"])

    jobs = asyncio.run(jobs_by_start(sessions))
    asyncio.run(engine.dispose())
    assert result.exit_code == 0, result.output
    assert "Requeued 1 abandoned backfill window" in result.output
    assert jobs[POISON_START].status == JobStatus.completed
