from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import main
from app.core.config import Settings
from app.google_health.data_types import REGISTRY
from app.models.state import JobStatus, SyncState
from app.sync.incremental import IncrementalSync


async def test_startup_fails_sync_states_left_running_by_a_dead_process(sessions, monkeypatch):
    async with sessions.begin() as session:
        session.add_all(
            [
                SyncState(
                    data_type="steps",
                    status=JobStatus.running,
                    last_page_token="resume-me",
                    last_requested_from=datetime(2026, 1, 1, tzinfo=UTC),
                    last_requested_to=datetime(2026, 1, 2, tzinfo=UTC),
                ),
                SyncState(data_type="sleep", status=JobStatus.completed),
            ]
        )
    settings = Settings(_env_file=None, app_env="test")
    incremental = IncrementalSync(
        settings, sessions, None, {"steps": REGISTRY["steps"], "sleep": REGISTRY["sleep"]}
    )
    runtime = SimpleNamespace(incremental=incremental, shutdown=AsyncMock(), scheduler=None)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "build_runtime", lambda *_: runtime)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    application = main.create_app()

    async with application.router.lifespan_context(application):
        async with sessions() as session:
            steps = await session.get(SyncState, "steps")
            sleep = await session.get(SyncState, "sleep")

    assert steps.status == JobStatus.failed
    assert steps.last_error == "process restarted"
    # The interrupted window stays resumable.
    assert steps.last_page_token == "resume-me"
    assert sleep.status == JobStatus.completed
    assert sleep.last_error is None
    assert await incremental.recover_interrupted() == 0
