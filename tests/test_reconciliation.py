from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from reconciliation_records import Client, service

from app.core.config import Settings
from app.google_health.client import Page
from app.google_health.data_types import REGISTRY
from app.services.reconciliation_reports import list_runs
from app.services.scheduler import build_scheduler
from app.services.storage import StoreStats
from app.sync.reconciliation import ReconciliationService


async def test_reconciliation_runs_every_configured_type_with_lookback():
    class Incremental:
        client = SimpleNamespace(warm_up=AsyncMock())
        specs = {"steps": object(), "sleep": object()}
        calls = []

        async def run(self, data_type, start, end=None):
            self.calls.append((data_type, start))
            return data_type

    incremental = Incremental()
    result = await ReconciliationService(incremental).run(7)
    assert result == {"steps": "steps", "sleep": "sleep"}
    assert [call[0] for call in incremental.calls] == ["steps", "sleep"]
    assert all(isinstance(call[1], datetime) for call in incremental.calls)


async def test_reconciliation_chunks_rollup_types_to_api_limit():
    class Incremental:
        client = SimpleNamespace(warm_up=AsyncMock())
        specs = {"total-calories": REGISTRY["total-calories"]}

        def __init__(self):
            self.calls = []

        async def run(self, data_type, start, end=None):
            self.calls.append((start, end or datetime.now(UTC)))
            return StoreStats(inserted=1)

    incremental = Incremental()
    result = await ReconciliationService(incremental).run(90)

    assert result["total-calories"].inserted == 7
    assert len(incremental.calls) == 7
    assert all(end - start <= timedelta(days=14) for start, end in incremental.calls)


@pytest.mark.parametrize("job_id", ["daily-reconciliation", "weekly-reconciliation"])
async def test_scheduled_entry_points_persist_the_requested_range(sessions, job_id):
    runner = service(sessions, Client(Page([], None)))
    scheduler = build_scheduler(Settings(_env_file=None), runner)
    job = scheduler.get_job(job_id)
    result = await job.func(**job.kwargs)
    assert isinstance(result["heart-rate"], StoreStats)
    async with sessions() as session:
        row = (await list_runs(session))["runs"][0]
    assert row["status"] == "completed"
    assert row["requested_to"] - row["requested_from"] == timedelta(days=job.kwargs["days"])


def test_cli_reconcile_uses_persistent_service_without_new_options(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from typer.testing import CliRunner

    from app.cli import main as cli
    from app.db.base import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cli.sqlite'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def prepare():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(prepare())
    closed = []

    async def shutdown():
        closed.append(True)

    runtime = SimpleNamespace(reconciliation=service(sessions), shutdown=shutdown)
    monkeypatch.setattr(cli, "build_runtime", lambda *_: runtime)
    try:
        result = CliRunner().invoke(cli.app, ["reconcile", "--days", "2"])
        assert result.exit_code == 0, result.output
        assert "StoreStats" in result.output and closed == [True]

        async def read():
            async with sessions() as session:
                row = (await list_runs(session))["runs"][0]
                assert row["status"] == "completed"
                assert row["requested_to"] - row["requested_from"] == timedelta(days=2)

        asyncio.run(read())
    finally:
        asyncio.run(engine.dispose())
