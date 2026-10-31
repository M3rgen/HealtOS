"""Synthetic webhook windows and archive snapshots for independent owners."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from retrieval_records import END, START
from sqlalchemy import select, update
from sync_ownership_records import service
from sync_ownership_records import snapshot as sync_snapshot
from test_webhook_route import notification

from app.core.config import Settings
from app.models.state import (
    OAuthToken,
    SyncState,
    WebhookBatchItem,
    WebhookEvent,
    WebhookNotification,
)
from app.models.webhook_status import WebhookStatusEvent
from app.parsers.common import payload_hash
from app.services.webhook_processing import WebhookBatchProcessor
from app.services.webhook_status import WebhookStatusService
from app.sync.incremental import STALE_RUNNING_AFTER


def processor(sessions, client):
    incremental = service(sessions, client)
    incremental.specs["heart-rate"] = replace(incremental.specs["heart-rate"], max_window_days=1)
    status = WebhookStatusService(Settings(_env_file=None), sessions)
    return WebhookBatchProcessor(sessions, incremental, set(incremental.specs), status)


async def ingest(runner, *, windows=1, kind="heart-rate", start=START):
    async with runner.sessions.begin() as session:
        if await session.get(OAuthToken, "me") is None:
            session.add(
                OAuthToken(
                    subject="me", encrypted_refresh_token="synthetic", health_user_id="user-1"
                )
            )
    payload = notification(kind)
    physical = payload["data"]["intervals"][0]["physicalTimeInterval"]
    physical.update(
        startTime=start.isoformat(), endTime=(start + (END - START) * windows).isoformat()
    )
    body = json.dumps(payload, indent=2).encode() + b"\n"
    batch = await runner.ingest_verified(body, payload)
    return batch.batch_hash, payload_hash(payload)


async def age(sessions):
    stale = datetime.now(UTC) - STALE_RUNNING_AFTER - timedelta(minutes=1)
    async with sessions.begin() as session:
        for model in (WebhookNotification, SyncState):
            await session.execute(update(model).values(updated_at=stale))


async def snapshot(sessions):
    result = await sync_snapshot(sessions)
    async with sessions() as session:
        for model in (WebhookNotification, WebhookEvent, WebhookBatchItem, WebhookStatusEvent):
            table = model.__table__
            rows = await session.execute(select(table).order_by(*table.primary_key.columns))
            result[table.name] = [dict(row) for row in rows.mappings()]
    return result
