import logging
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from reconciliation_records import END, Client, Frozen, point, report, service

from app.core.timeutil import as_utc
from app.google_health.client import GoogleHealthError, Page, RetryDeferredError
from app.google_health.data_types import REGISTRY
from app.google_health.oauth import OAuthError
from app.models.state import JobStatus, SyncState
from app.services.reconciliation_reports import recovery_errors
from app.sync import reconciliation


@pytest.fixture(autouse=True)
def enable_events(monkeypatch):
    # Alembic's fileConfig disables loggers created before the migration tests.
    for name in ("app.sync.incremental", "app.sync.reconciliation"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)


@pytest.mark.parametrize("partial", [False, True])
async def test_final_pass_recovers_first_type_and_preserves_checkpoints(
    sessions, monkeypatch, caplog, partial
):
    monkeypatch.setattr(reconciliation, "datetime", Frozen)
    previous = END + timedelta(days=1)
    async with sessions.begin() as session:
        session.add(SyncState(data_type="heart-rate", last_successful_sync=previous))
    pages = [Page([point()], "next")] if partial else []
    client = Client(
        *pages,
        OAuthError("connection failed", transient=True),
        Page([], None),
        Page([], None),
    )
    client.warm_up = AsyncMock()
    runner = service(sessions, client, {k: REGISTRY[k] for k in ("heart-rate", "steps")})
    run_id = uuid.uuid4()
    with caplog.at_level(logging.INFO):
        result = await runner.run(1, run_id=run_id)
    client.warm_up.assert_awaited_once()
    assert not any(isinstance(value, Exception) for value in result.values())
    assert client.tokens == ([None, "next", None, "next"] if partial else [None, None, None])
    persisted = await report(sessions, run_id)
    heart = next(row for row in persisted["types"] if row["data_type"] == "heart-rate")
    assert persisted["status"] == heart["status"] == "completed"
    assert persisted["error_type"] is heart["error_type"] is None
    assert (heart["inserted"], heart["pages_committed"]) == (int(partial), 2 if partial else 1)
    assert await recovery_errors(sessions, run_id) == ()
    async with sessions() as session:
        state = await session.get(SyncState, "heart-rate")
        assert state.status == JobStatus.completed and state.last_error is None
        assert as_utc(state.last_successful_sync) == previous and state.last_page_token is None
    failed = [r for r in caplog.records if getattr(r, "event", None) == "SYNC_FAILED"]
    assert len(failed) == 1 and failed[0].transient is True
    completed = next(
        r for r in caplog.records if getattr(r, "event", None) == "RECONCILIATION_COMPLETED"
    )
    assert completed.failure_count == 0


@pytest.mark.parametrize(
    "failure,transient",
    [
        (OAuthError("HTTP 401"), False),
        (GoogleHealthError("HTTP 403"), False),
        (RuntimeError("synthetic"), False),
        (OAuthError("connection failed", transient=True), True),
        (GoogleHealthError("connection failed after retries"), True),
        (GoogleHealthError("request failed after retries (HTTP 503)"), True),
        (GoogleHealthError("request failed after retries (HTTP 429)"), True),
    ],
)
async def test_only_transient_types_get_one_final_attempt(sessions, caplog, failure, transient):
    client = Client(failure, Page([], None), *([failure] if transient else []))
    runner = service(sessions, client, {k: REGISTRY[k] for k in ("heart-rate", "steps")})
    run_id = uuid.uuid4()
    with caplog.at_level(logging.INFO):
        result = await runner.run(1, run_id=run_id)
    assert result["heart-rate"] is failure and len(client.tokens) == 2 + int(transient)
    if transient:
        runner.sleep.assert_awaited_once_with(reconciliation.FINAL_PASS_DELAY_SECONDS)
    else:
        runner.sleep.assert_not_awaited()
    persisted = await report(sessions, run_id)
    assert persisted["status"] == "partial"
    assert await recovery_errors(sessions, run_id) == (type(failure).__name__,)
    failed = [r for r in caplog.records if getattr(r, "event", None) == "SYNC_FAILED"]
    assert len(failed) == 1 + int(transient) and all(r.transient == transient for r in failed)
    completed = next(
        r for r in caplog.records if getattr(r, "event", None) == "RECONCILIATION_COMPLETED"
    )
    assert completed.failure_count == 1


async def test_deferred_retry_after_gets_no_early_final_attempt(sessions):
    failure = RetryDeferredError("Google Health API retry deferred (HTTP 429)")
    client = Client(failure, Page([], None))
    runner = service(sessions, client, {k: REGISTRY[k] for k in ("heart-rate", "steps")})
    result = await runner.run(1)
    assert result["heart-rate"] is failure and len(client.tokens) == 2
    runner.sleep.assert_not_awaited()
