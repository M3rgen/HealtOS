from types import SimpleNamespace

import pytest

from app.api.sync import _serialize
from app.core.config import Settings
from app.google_health.client import GoogleHealthError, InvalidPageTokenError
from app.google_health.data_types import REGISTRY
from app.models.state import JobStatus, SyncState
from app.sync.errors import error_summary
from app.sync.incremental import IncrementalSync


@pytest.mark.parametrize(
    ("error", "summary"),
    [
        (
            GoogleHealthError("Google Health API returned HTTP 403: PERMISSION_DENIED"),
            "GoogleHealthError (http_403)",
        ),
        (
            GoogleHealthError("Google Health API request failed after retries (HTTP 503)"),
            "GoogleHealthError (http_503)",
        ),
        (
            GoogleHealthError("Google Health API connection failed after retries"),
            "GoogleHealthError (connection)",
        ),
        (
            InvalidPageTokenError("Google Health pagination token expired"),
            "InvalidPageTokenError (invalid_page_token)",
        ),
        (RuntimeError("upstream said something private"), "RuntimeError"),
    ],
)
def test_error_summary_is_class_name_and_short_code(error, summary):
    assert error_summary(error) == summary


async def test_sync_status_exposes_error_summary_not_exception_text(sessions):
    class RejectingClient:
        async def list_data_points(self, spec, start, end, page_token=None):
            raise GoogleHealthError("Google Health API returned HTTP 403: detail from upstream")

    service = IncrementalSync(Settings(), sessions, RejectingClient(), {"steps": REGISTRY["steps"]})
    with pytest.raises(GoogleHealthError):
        await service.run("steps")
    async with sessions() as session:
        state = await session.get(SyncState, "steps")

    payload = _serialize(state, SimpleNamespace(incremental=service))
    assert payload["last_error"] == "GoogleHealthError (http_403)"
    assert "upstream" not in payload["last_error"]


async def test_receipt_start_failure_fails_the_sync_state(sessions, monkeypatch):
    from unittest.mock import AsyncMock

    from sqlalchemy.exc import OperationalError

    from app.sync import incremental

    class CountingClient:
        calls = 0

        async def list_data_points(self, spec, start, end, page_token=None):
            CountingClient.calls += 1
            raise AssertionError("no fetch without a receipt")

    failure = AsyncMock()
    monkeypatch.setattr(
        incremental.ReceiptWriter,
        "begin",
        AsyncMock(side_effect=OperationalError("INSERT", {}, Exception("synthetic"))),
    )
    monkeypatch.setattr(incremental.ReceiptWriter, "failure", failure)
    service = IncrementalSync(Settings(), sessions, CountingClient(), {"steps": REGISTRY["steps"]})

    with pytest.raises(OperationalError):
        await service.run("steps")
    async with sessions() as session:
        state = await session.get(SyncState, "steps")

    # The run's own handler fails the state, so nothing stays `running` without an owner.
    assert state.status == JobStatus.failed
    assert state.last_error == "OperationalError"
    assert service.last_runs["steps"]["failed"] is True
    assert CountingClient.calls == 0
    # No receipt row exists, so no outcome event is written for it.
    failure.assert_not_awaited()
