from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import main
from app.core.config import Settings
from app.google_health.data_types import REGISTRY
from app.models.state import JobStatus, SyncState, WebhookNotification
from app.services.webhook_processing import INTERRUPTED_ERROR, WebhookBatchProcessor
from app.sync.incremental import IncrementalSync


def start_app(sessions, monkeypatch, settings, incremental):
    runtime = SimpleNamespace(
        incremental=incremental,
        webhook_processor=WebhookBatchProcessor(sessions, incremental, set(incremental.specs)),
        shutdown=AsyncMock(),
        scheduler=None,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "build_runtime", lambda *_: runtime)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    application = main.create_app()
    return application.router.lifespan_context(application)


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
    async with start_app(sessions, monkeypatch, settings, incremental):
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


def element(element_hash, status):
    return WebhookNotification(
        element_hash=element_hash,
        raw_json={"data": {"dataType": "steps"}},
        is_valid=True,
        data_type="steps",
        health_user_id="user-1",
        operation="UPSERT",
        status=status,
        attempt_count=1,
    )


async def test_startup_fails_webhook_elements_left_running_by_a_dead_process(sessions, monkeypatch):
    async with sessions.begin() as session:
        session.add_all([element("crashed", "running"), element("done", "completed")])
    settings = Settings(_env_file=None, app_env="test")
    incremental = IncrementalSync(settings, sessions, None, {"steps": REGISTRY["steps"]})

    async with start_app(sessions, monkeypatch, settings, incremental):
        async with sessions() as session:
            crashed = await session.get(WebhookNotification, "crashed")
            done = await session.get(WebhookNotification, "done")

    # failed is claimable again, so the next delivery or replay retries the element.
    assert crashed.status == "failed"
    assert crashed.last_error == INTERRUPTED_ERROR
    assert crashed.attempt_count == 1
    assert done.status == "completed"
    assert done.last_error is None
