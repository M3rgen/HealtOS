"""Metadata-created diagnostic history is immutable and its guards are verified."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.models.webhook_status import SQLITE_TRIGGER_SQL, TABLE
from app.services.raw_history import verify_guards
from app.services.webhook_status import WebhookStatusService


async def test_metadata_guards_allow_service_inserts_and_refuse_all_changes(sessions):
    service = WebhookStatusService(
        Settings(), sessions, clock=lambda: datetime(2026, 10, 1, tzinfo=UTC)
    )
    await service.record("signature_valid", "invalid_signature")
    async with sessions() as session:
        before = (await session.execute(text(f"SELECT * FROM {TABLE} ORDER BY id"))).all()
        assert len(before) == 2
        assert await verify_guards(session) == []
    for statement in (
        f"UPDATE {TABLE} SET reason = 'challenge'",
        f"UPDATE {TABLE} SET occurred_at = '2000-01-01'",
        f"UPDATE {TABLE} SET id = id + 100",
        f"DELETE FROM {TABLE}",
    ):
        with pytest.raises(DBAPIError, match="keeps every record"):
            async with sessions.begin() as session:
                await session.execute(text(statement))
        async with sessions() as session:
            assert (
                await session.execute(text(f"SELECT * FROM {TABLE} ORDER BY id"))
            ).all() == before
    await service.record("challenge")
    assert (await service.snapshot())["counters"]["reasons"]["challenge"] == 1


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
@pytest.mark.parametrize("tampering", ["missing", "redefined"])
async def test_verify_reports_missing_or_redefined_sqlite_guards(sessions, operation, tampering):
    trigger = f"{TABLE}_no_{operation.lower()}"
    async with sessions.begin() as session:
        await session.execute(text(f"DROP TRIGGER {trigger}"))
        if tampering == "redefined":
            # It still has the expected name and body but never fires.
            sql = SQLITE_TRIGGER_SQL[trigger][1].replace("\nBEGIN", " WHEN 0\nBEGIN")
            await session.execute(text(sql))
    async with sessions() as session:
        expected = (
            f"protective trigger missing: {TABLE}.{trigger}"
            if tampering == "missing"
            else f"protective trigger {TABLE}.{trigger} was redefined"
        )
        assert await verify_guards(session) == [expected]
