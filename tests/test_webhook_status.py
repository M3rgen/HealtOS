"""Synthetic-clock projections, durable counters and transactional observations."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import webhooks
from app.core.config import Settings
from app.core.request_limits import WriteLimits
from app.db.base import Base
from app.models.state import WebhookBatchItem, WebhookEvent, WebhookNotification
from app.models.webhook_status import WebhookStatusEvent
from app.services.webhook_status import (
    SAMPLE_INTERVAL,
    SILENCE_THRESHOLD,
    WebhookStatusService,
)

START = datetime(2026, 10, 1, tzinfo=UTC)


def configured():
    return Settings(
        _env_file=None,
        google_health_webhook_endpoint_uri="https://hooks.example/webhooks/google-health",
        google_health_webhook_authorization="Bearer synthetic-test",
    )


@pytest.mark.parametrize("received", [False, True])
async def test_silence_threshold_invalid_traffic_and_restart(sessions, received):
    now = START
    status = WebhookStatusService(configured(), sessions, lambda: now)
    await status.start()
    if received:
        await status.record("signature_valid", "notification_received")
    initial = await status.snapshot()
    assert initial["silence"]["state"] == ("receiving" if received else "never_received")
    assert initial["silence"]["warning"] is False
    assert initial["last_notification_at"] == (START if received else None)
    now += SILENCE_THRESHOLD - timedelta(microseconds=1)
    assert (await status.snapshot())["silence"]["warning"] is False
    now += timedelta(microseconds=1)
    await status.record("authorization_rejected", "missing_signature", "challenge")
    await status.record("signature_valid", "expired_replay")
    boundary = await status.snapshot()
    assert boundary["silence"] == {
        "state": "silent",
        "warning": True,
        "since": START,
        "never_received": not received,
        "threshold_seconds": 604800,
    }
    # New service represents a process restart; the grace period and all counters persist.
    restarted = WebhookStatusService(configured(), sessions, lambda: now)
    await restarted.start()
    after = await restarted.snapshot()
    assert after["silence"] == boundary["silence"]
    assert after["counters"] == boundary["counters"]
    assert after["last_notification_at"] == boundary["last_notification_at"]
    await restarted.record("signature_valid", "notification_received", "duplicate")
    assert (await restarted.snapshot())["silence"]["state"] == "receiving"


async def test_unconfigured_time_does_not_age_new_receiver(sessions):
    now = START
    settings = Settings(_env_file=None)
    status = WebhookStatusService(settings, sessions, lambda: now)
    await status.start()
    now += timedelta(days=40)
    absent = await status.snapshot()
    assert absent["silence"]["state"] == "unconfigured"
    assert absent["silence"]["warning"] is False
    assert absent["silence"]["since"] is None
    ready = WebhookStatusService(configured(), sessions, lambda: now)
    await ready.start()
    result = await ready.snapshot()
    assert result["silence"]["state"] == "never_received"
    assert result["silence"]["since"] == now
    assert result["counters"]["since"] == START
    assert result["subscription"] == {
        "status": "unknown",
        "renewal_status": "unknown",
        "last_renewed_at": None,
        "basis": "no_subscription_client_or_evidence",
    }


async def test_archived_verification_is_read_only_evidence_not_a_counter_backfill(sessions):
    async with sessions.begin() as session:
        session.add_all(
            [
                WebhookEvent(
                    payload_hash="a" * 64,
                    raw_json={},
                    raw_body=b"{}",
                    signature_verified_at=START,
                    last_error="private upstream error",
                ),
                # Legacy materialization without signed body is not delivery evidence.
                WebhookEvent(
                    payload_hash="b" * 64,
                    raw_json={},
                    signature_verified_at=START + timedelta(days=8),
                ),
            ]
        )
        await session.flush()
        session.add(WebhookNotification(element_hash="c" * 64, raw_json={}, is_valid=True))
        await session.flush()
        session.add(WebhookBatchItem(batch_hash="a" * 64, ordinal=1, element_hash="c" * 64))
    status = WebhookStatusService(configured(), sessions, lambda: START + timedelta(days=8))
    await status.start()
    snapshot = await status.snapshot()
    assert snapshot["last_notification_at"] == START
    assert snapshot["silence"]["warning"] is True
    assert snapshot["counters"]["valid"] == 0
    assert snapshot["last_error"] is None
    async with sessions() as session:
        assert (await session.get(WebhookEvent, "a" * 64)).last_error == "private upstream error"


async def test_append_only_observations_rollback_with_processing_transaction(sessions):
    status = WebhookStatusService(configured(), sessions, lambda: START)
    await status.start()
    with pytest.raises(RuntimeError):
        async with sessions.begin() as session:
            status.observe(session, "notification_received", "duplicate")
            await session.flush()
            raise RuntimeError("synthetic rollback")
    snapshot = await status.snapshot()
    assert snapshot["counters"]["reasons"]["notification_received"] == 0
    assert snapshot["last_notification_at"] is None
    with pytest.raises(ValueError, match="Unknown"):
        await status.record("private-error-string")


async def test_concurrent_writers_from_two_runtimes_lose_no_increments(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'counters.db'}")
    sessions = async_sessionmaker(engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        services = [WebhookStatusService(configured(), sessions, lambda: START) for _ in range(2)]
        await services[0].start()
        clients = []
        for status in services:
            app = FastAPI()
            app.state.settings = status.settings
            app.state.runtime = SimpleNamespace(webhook_status=status)
            app.state.write_limits = WriteLimits(app.state.settings)
            app.include_router(webhooks.router)
            clients.append(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://localhost"
                )
            )
        # Authorized but unsigned: pre-authorization reasons are sampled by design, so the
        # no-lost-increment invariant is checked on a reason that is always persisted.
        async with clients[0], clients[1]:
            responses = await asyncio.gather(
                *(
                    clients[i % 2].post(
                        "/webhooks/google-health",
                        content=b"{}",
                        headers={"Authorization": "Bearer synthetic-test"},
                    )
                    for i in range(40)
                )
            )
        assert all(response.status_code == 401 for response in responses)
        snapshot = await services[1].snapshot()
        assert snapshot["counters"]["reasons"]["missing_signature"] == 40
        assert snapshot["counters"]["scope"] == "persistent_diagnostic_events"
        assert snapshot["counters"]["survives_restart"] is True
        # Repeated GET projections never insert diagnostic rows.
        await services[1].snapshot()
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(WebhookStatusEvent)) == 42
    finally:
        await engine.dispose()


async def test_pre_authorization_rejections_persist_one_row_per_reason_per_minute(sessions):
    now = START
    status = WebhookStatusService(configured(), sessions, lambda: now)
    for _ in range(3):
        await status.record("authorization_rejected")
        await status.record("receiver_unconfigured")
        await status.record("missing_signature")
    now += SAMPLE_INTERVAL - timedelta(microseconds=1)
    await status.record("authorization_rejected")
    now += timedelta(microseconds=1)
    await status.record("authorization_rejected")
    snapshot = await status.snapshot()
    reasons = snapshot["counters"]["reasons"]
    assert reasons["authorization_rejected"] == 2
    assert reasons["receiver_unconfigured"] == 1
    # Authorized-stage reasons are never sampled.
    assert reasons["missing_signature"] == 3
    assert snapshot["process_local"]["unpersisted"] == {
        "authorization_rejected": 3,
        "receiver_unconfigured": 2,
    }
    assert snapshot["process_local"]["survives_restart"] is False
    assert snapshot["last_error"] == {
        "code": "authorization_rejected",
        "stage": "validation",
        "at": now,
    }
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(WebhookStatusEvent)) == 6
    # A new process starts with empty memory; persisted rows remain.
    restarted = WebhookStatusService(configured(), sessions, lambda: now)
    after = await restarted.snapshot()
    assert after["counters"]["reasons"] == reasons
    assert after["process_local"]["unpersisted"] == {
        "authorization_rejected": 0,
        "receiver_unconfigured": 0,
    }
