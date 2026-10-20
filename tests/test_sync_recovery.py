from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import main
from app.core.config import Settings
from app.core.timeutil import as_utc
from app.google_health.client import Page
from app.google_health.data_types import REGISTRY
from app.models.state import JobStatus, SyncState, WebhookNotification
from app.services.webhook_processing import INTERRUPTED_ERROR, WebhookBatchProcessor
from app.sync.incremental import STALE_RUNNING_AFTER, IncrementalSync

# Last heartbeat long enough ago that no live process can still own the row.
STALE = datetime.now(UTC) - STALE_RUNNING_AFTER - timedelta(minutes=1)


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
                    updated_at=STALE,
                ),
                SyncState(data_type="sleep", status=JobStatus.completed),
                # Heartbeat a moment ago: a `healthos sync` in another container.
                SyncState(data_type="weight", status=JobStatus.running),
            ]
        )
    settings = Settings(_env_file=None, app_env="test")
    incremental = IncrementalSync(
        settings,
        sessions,
        None,
        {name: REGISTRY[name] for name in ("steps", "sleep", "weight")},
    )
    async with start_app(sessions, monkeypatch, settings, incremental):
        async with sessions() as session:
            steps = await session.get(SyncState, "steps")
            sleep = await session.get(SyncState, "sleep")
            weight = await session.get(SyncState, "weight")

    assert steps.status == JobStatus.failed
    assert steps.last_error == "process restarted"
    # The interrupted window stays resumable.
    assert steps.last_page_token == "resume-me"
    assert sleep.status == JobStatus.completed
    assert sleep.last_error is None
    assert weight.status == JobStatus.running
    assert await incremental.recover_interrupted() == 0
    # Once its heartbeat is older than the grace period, it is recovered too.
    later = datetime.now(UTC) + STALE_RUNNING_AFTER + timedelta(minutes=1)
    assert await incremental.recover_interrupted(now=later) == 1


def element(element_hash, status, data_type="steps", **fields):
    return WebhookNotification(
        element_hash=element_hash,
        raw_json={"data": {"dataType": "steps"}},
        is_valid=True,
        data_type=data_type,
        health_user_id="user-1",
        operation="UPSERT",
        status=status,
        attempt_count=1,
        **fields,
    )


async def test_startup_fails_webhook_elements_left_running_by_a_dead_process(sessions, monkeypatch):
    async with sessions.begin() as session:
        session.add_all(
            [
                element("crashed", "running", updated_at=STALE),
                element("done", "completed"),
                # Another process just claimed it.
                element("claimed", "running"),
                # Claimed long ago, but its fetch is still paging (fresh sync heartbeat).
                element("paging", "running", data_type="sleep", updated_at=STALE),
                SyncState(data_type="sleep", status=JobStatus.running),
            ]
        )
    settings = Settings(_env_file=None, app_env="test")
    incremental = IncrementalSync(
        settings, sessions, None, {"steps": REGISTRY["steps"], "sleep": REGISTRY["sleep"]}
    )

    async with start_app(sessions, monkeypatch, settings, incremental):
        async with sessions() as session:
            crashed = await session.get(WebhookNotification, "crashed")
            done = await session.get(WebhookNotification, "done")
            claimed = await session.get(WebhookNotification, "claimed")
            paging = await session.get(WebhookNotification, "paging")

    # failed is claimable again, so the next delivery or replay retries the element.
    assert crashed.status == "failed"
    assert crashed.last_error == INTERRUPTED_ERROR
    assert crashed.attempt_count == 1
    assert done.status == "completed"
    assert done.last_error is None
    assert claimed.status == "running"
    assert paging.status == "running"


class AgingClient:
    """Three empty pages; ages the heartbeat between them like a slow page would."""

    def __init__(self, sessions):
        self.sessions = sessions
        self.calls = 0
        self.heartbeat = None

    async def list_data_points(self, spec, start, end, page_token=None):
        self.calls += 1
        async with self.sessions.begin() as session:
            state = await session.get(SyncState, spec.name)
            if self.calls == 2:
                state.updated_at = STALE
            if self.calls == 3:
                self.heartbeat = as_utc(state.updated_at)
        return Page([], f"p{self.calls + 1}" if self.calls < 3 else None)


async def test_a_paging_sync_refreshes_its_heartbeat_on_every_page(sessions):
    client = AgingClient(sessions)
    incremental = IncrementalSync(Settings(), sessions, client, {"steps": REGISTRY["steps"]})
    start = datetime(2026, 8, 1, tzinfo=UTC)

    await incremental.run("steps", start, start + timedelta(days=1))

    # The second page stored nothing, yet the row was refreshed before the third request.
    assert client.heartbeat > STALE + timedelta(minutes=1)
    assert await incremental.recover_interrupted() == 0
