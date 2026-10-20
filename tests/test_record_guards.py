"""Manual entries and webhook records can be added, never deleted or rewritten (SQLite)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.models.manual import ManualMeasurement, ManualWorkout
from app.models.state import WebhookBatchItem, WebhookEvent, WebhookNotification

NOW = datetime(2026, 10, 20, 8, tzinfo=UTC)


async def refused(sessions, statement: str) -> None:
    with pytest.raises((IntegrityError, OperationalError), match="refused"):
        async with sessions.begin() as session:
            await session.execute(text(statement))


async def test_manual_entries_are_append_only(sessions) -> None:
    async with sessions.begin() as session:
        session.add(ManualMeasurement(metric="weight", value=Decimal("70.5"), measured_at=NOW))
        session.add(ManualWorkout(exercise_type="RUN", start_time=NOW, end_time=NOW))
    for statement in (
        "UPDATE manual_measurements SET value = 99",
        "DELETE FROM manual_measurements",
        "UPDATE manual_workouts SET exercise_type = 'WALK'",
        "DELETE FROM manual_workouts",
    ):
        await refused(sessions, statement)
    async with sessions() as session:
        assert (await session.scalar(select(ManualMeasurement.value))) == Decimal("70.5")


async def test_webhook_payloads_are_frozen_but_processing_state_is_not(sessions) -> None:
    async with sessions.begin() as session:
        session.add(
            WebhookEvent(payload_hash="b1", raw_json={"data": 1}, raw_body=None, element_count=1)
        )
        session.add(WebhookNotification(element_hash="e1", raw_json={"data": 1}))
    async with sessions.begin() as session:
        session.add(WebhookBatchItem(batch_hash="b1", ordinal=1, element_hash="e1"))
    async with sessions.begin() as session:
        # Processing state and a legacy batch's first signed body may still be written.
        await session.execute(
            text("UPDATE webhook_events SET failure_count = 1, raw_body = x'7b7d'")
        )
        await session.execute(text("UPDATE webhook_notifications SET status = 'completed'"))
    for statement in (
        "UPDATE webhook_events SET raw_body = x'5b5d'",
        "UPDATE webhook_events SET raw_json = '{}'",
        "UPDATE webhook_events SET payload_hash = 'x'",
        "DELETE FROM webhook_events",
        "UPDATE webhook_notifications SET raw_json = '{}'",
        "DELETE FROM webhook_notifications",
        "DELETE FROM webhook_batch_items",
    ):
        await refused(sessions, statement)
