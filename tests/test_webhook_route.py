import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.api import webhooks
from app.core.config import Settings
from app.core.request_limits import WriteLimits
from app.google_health.data_types import REGISTRY
from app.models.state import OAuthToken, WebhookBatchItem, WebhookEvent, WebhookNotification
from app.parsers.common import payload_hash
from app.services.webhook_processing import WebhookBatchProcessor
from app.services.webhook_status import WebhookStatusService


class Verifier:
    def __init__(self):
        self.bodies = []

    async def verify(self, body, signature):
        assert signature == "signed"
        self.bodies.append(body)


class Incremental:
    specs = {"steps": REGISTRY["steps"], "heart-rate": REGISTRY["heart-rate"]}

    def __init__(self, fail_once=()):
        self.calls = []
        self.fail_once = set(fail_once)

    async def run(self, data_type, start=None, end=None, *, check_owner=None):
        self.calls.append((data_type, start, end))
        if data_type in self.fail_once:
            self.fail_once.remove(data_type)
            raise RuntimeError(f"temporary {data_type} failure")


def notification(data_type="steps", user="user-1", minute=0):
    return {
        "data": {
            "dataType": data_type,
            "healthUserId": user,
            "operation": "UPSERT",
            "intervals": [
                {
                    "physicalTimeInterval": {
                        "startTime": f"2026-08-17T10:{minute:02d}:00Z",
                        "endTime": f"2026-08-17T10:{minute + 1:02d}:00Z",
                    }
                }
            ],
        }
    }


async def make_client(sessions, incremental=None):
    async with sessions.begin() as session:
        session.add(
            OAuthToken(
                subject="me",
                encrypted_refresh_token="encrypted",
                health_user_id="user-1",
            )
        )
    verifier = Verifier()
    incremental = incremental or Incremental()
    processor = WebhookBatchProcessor(sessions, incremental, set(incremental.specs))
    tasks = []

    def spawn(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    application = FastAPI()
    application.state.settings = Settings(google_health_webhook_authorization="Bearer local-test")
    application.state.runtime = SimpleNamespace(
        webhook_verifier=verifier,
        webhook_processor=processor,
        spawn=spawn,
        webhook_status=WebhookStatusService(application.state.settings, sessions),
    )
    application.state.write_limits = WriteLimits(application.state.settings)
    application.include_router(webhooks.router)
    transport = httpx.ASGITransport(app=application)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return client, verifier, incremental, processor, tasks


async def post_signed(client, payload):
    body = json.dumps(payload, separators=(",", ":")).encode()
    response = await client.post(
        "/webhooks/google-health",
        content=body,
        headers={
            "Authorization": "Bearer local-test",
            "GOOGLE-HEALTH-API-SIGNATURE": "signed",
            "Content-Type": "application/json",
        },
    )
    return response, body


async def finish(tasks):
    await asyncio.gather(*tasks)
    tasks.clear()


async def test_single_object_payload_and_exact_raw_body(sessions):
    client, verifier, incremental, _, tasks = await make_client(sessions)
    payload = notification()
    async with client:
        response, body = await post_signed(client, payload)
        await finish(tasks)
    async with sessions() as session:
        batch = await session.scalar(select(WebhookEvent))
        element = await session.scalar(select(WebhookNotification))
    assert response.status_code == 204
    assert verifier.bodies == [body]
    assert batch.raw_body == body
    assert batch.raw_json == payload
    assert batch.element_count == 1
    assert batch.processed_at is not None
    assert element.data_type == "steps"
    assert element.health_user_id == "user-1"
    assert element.operation == "UPSERT"
    assert element.intervals == payload["data"]["intervals"]
    assert incremental.calls == [
        (
            "steps",
            datetime(2026, 8, 17, 10, tzinfo=UTC),
            datetime(2026, 8, 17, 10, 1, tzinfo=UTC),
        )
    ]


async def test_one_element_array(sessions):
    client, _, incremental, _, tasks = await make_client(sessions)
    async with client:
        response, _ = await post_signed(client, [notification()])
        await finish(tasks)
    assert response.status_code == 204
    assert [call[0] for call in incremental.calls] == ["steps"]


async def test_multi_element_array_with_mixed_data_types(sessions):
    client, _, incremental, _, tasks = await make_client(sessions)
    payload = [notification("steps", minute=0), notification("heart-rate", minute=2)]
    async with client:
        response, _ = await post_signed(client, payload)
        await finish(tasks)
    async with sessions() as session:
        batch = await session.scalar(select(WebhookEvent))
        elements = (await session.scalars(select(WebhookNotification))).all()
    assert response.status_code == 204
    assert batch.element_count == 2
    assert batch.processed_at is not None
    assert {element.data_type for element in elements} == {"steps", "heart-rate"}
    assert [call[0] for call in incremental.calls] == ["steps", "heart-rate"]


async def test_duplicate_batch_replay_does_not_process_elements_twice(sessions):
    client, verifier, incremental, _, tasks = await make_client(sessions)
    payload = [notification()]
    async with client:
        first, body = await post_signed(client, payload)
        await finish(tasks)
        duplicate, duplicate_body = await post_signed(client, payload)
        await finish(tasks)
    async with sessions() as session:
        batches = await session.scalar(select(func.count(WebhookEvent.payload_hash)))
        elements = await session.scalar(select(func.count(WebhookNotification.element_hash)))
    assert first.status_code == duplicate.status_code == 204
    assert body == duplicate_body
    assert verifier.bodies == [body, body]
    assert batches == elements == 1
    assert len(incremental.calls) == 1


async def test_invalid_health_user_is_rejected_before_sync(sessions):
    client, _, incremental, _, tasks = await make_client(sessions)
    async with client:
        response, _ = await post_signed(client, [notification(user="other-user")])
        await finish(tasks)
    async with sessions() as session:
        batch = await session.scalar(select(WebhookEvent))
        element = await session.scalar(select(WebhookNotification))
    assert response.status_code == 204
    assert incremental.calls == []
    assert element.status == "rejected"
    assert "does not match" in element.validation_error
    assert batch.failure_count == 1
    assert batch.processed_at is not None


async def test_malformed_array_element_is_recorded_without_losing_valid_element(sessions):
    client, _, incremental, _, tasks = await make_client(sessions)
    async with client:
        response, _ = await post_signed(client, ["malformed", notification()])
        await finish(tasks)
    async with sessions() as session:
        batch = await session.scalar(select(WebhookEvent))
        elements = (await session.scalars(select(WebhookNotification))).all()
    assert response.status_code == 204
    assert [element.status for element in elements].count("rejected") == 1
    assert [element.status for element in elements].count("completed") == 1
    assert batch.failure_count == 1
    assert batch.processed_at is not None
    assert len(incremental.calls) == 1


async def test_partial_batch_failure_preserves_success_and_retry_completes(sessions):
    incremental = Incremental(fail_once={"heart-rate"})
    client, _, incremental, processor, tasks = await make_client(sessions, incremental)
    payload = [notification("steps", minute=0), notification("heart-rate", minute=2)]
    async with client:
        response, _ = await post_signed(client, payload)
        await finish(tasks)
    async with sessions() as session:
        batch = await session.scalar(select(WebhookEvent))
        states = {
            row.data_type: (row.status, row.attempt_count)
            for row in (await session.scalars(select(WebhookNotification))).all()
        }
    assert response.status_code == 204
    assert batch.processed_at is None
    assert states == {"steps": ("completed", 1), "heart-rate": ("failed", 1)}

    result = await processor.process_pending(recover_running=False)
    async with sessions() as session:
        batch = await session.scalar(select(WebhookEvent))
        states = {
            row.data_type: (row.status, row.attempt_count)
            for row in (await session.scalars(select(WebhookNotification))).all()
        }
    assert result.failed_elements == 0
    assert batch.processed_at is not None
    assert states == {"steps": ("completed", 1), "heart-rate": ("completed", 2)}
    assert [call[0] for call in incremental.calls] == ["steps", "heart-rate", "heart-rate"]


async def test_handshake_still_requires_exact_authorization(sessions):
    client, verifier, _, _, _ = await make_client(sessions)
    async with client:
        unauthorized = await client.post("/webhooks/google-health", json={"type": "verification"})
        handshake = await client.post(
            "/webhooks/google-health",
            json={"type": "verification"},
            headers={"Authorization": "Bearer local-test"},
        )
    assert unauthorized.status_code == 401
    assert handshake.status_code == 201
    assert verifier.bodies == []


async def test_redelivered_signed_body_older_than_a_day_cannot_restart_processing(sessions):
    incremental = Incremental(fail_once={"steps"})
    client, _, incremental, processor, tasks = await make_client(sessions, incremental)
    payload = notification("steps")

    async def age_batch(hours):
        async with sessions.begin() as session:
            batch = await session.scalar(select(WebhookEvent))
            batch.signature_verified_at = datetime.now(UTC) - timedelta(hours=hours)

    async def element_status():
        async with sessions() as session:
            return (await session.scalar(select(WebhookNotification))).status

    async with client:
        await post_signed(client, payload)
        await finish(tasks)
        assert (len(incremental.calls), await element_status()) == (1, "failed")

        await age_batch(25)
        stale, _ = await post_signed(client, payload)
        await finish(tasks)
        assert stale.status_code == 204
        assert (len(incremental.calls), await element_status()) == (1, "failed")

        await age_batch(1)
        incremental.fail_once.add("steps")
        fresh, _ = await post_signed(client, payload)
        await finish(tasks)
        assert fresh.status_code == 204
        assert (len(incremental.calls), await element_status()) == (2, "failed")

    # The operator replay path (process_pending) is not limited by the age cap.
    await age_batch(48)
    await processor.process_pending(recover_running=False)
    assert (len(incremental.calls), await element_status()) == (3, "completed")


class GatedIncremental(Incremental):
    """Blocks inside the sync until the test opens the gate."""

    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def run(self, data_type, start=None, end=None, *, check_owner=None):
        self.calls.append((data_type, start, end))
        self.entered.set()
        await self.gate.wait()


async def test_element_locks_serialize_work_and_are_dropped_when_idle(sessions):
    incremental = GatedIncremental()
    _, _, _, processor, _ = await make_client(sessions, incremental)
    payload = notification("steps")
    raw = json.dumps(payload).encode()
    batch = await processor.ingest_verified(raw, payload)
    async with sessions() as session:
        element_hash = (await session.scalar(select(WebhookNotification))).element_hash

    holder = asyncio.create_task(processor.process_batch(batch.batch_hash))
    await asyncio.wait_for(incremental.entered.wait(), 2)
    waiters = [asyncio.create_task(processor.process_batch(batch.batch_hash)) for _ in range(2)]
    await asyncio.sleep(0.01)
    assert processor._element_locks[element_hash].users == 3
    # A waiter cancelled while queued must not drop the lock the holder still owns.
    waiters[0].cancel()
    await asyncio.gather(waiters[0], return_exceptions=True)
    assert processor._element_locks[element_hash].users == 2
    assert processor._element_locks[element_hash].lock.locked()

    incremental.gate.set()
    first = await asyncio.wait_for(holder, 2)
    second = await asyncio.wait_for(waiters[1], 2)
    assert (first.completed_elements, second.completed_elements) == (1, 1)
    # The queued call saw the completed element instead of syncing it again.
    assert len(incremental.calls) == 1
    assert processor._element_locks == {}


@pytest.mark.parametrize(
    "field,column,limit",
    [
        ("dataType", "data_type", 128),
        ("healthUserId", "health_user_id", 128),
        ("operation", "operation", 32),
    ],
)
@pytest.mark.parametrize("bad", [{}, [], 42, False, None, "", "ğ" * 129])
@pytest.mark.parametrize("shape", ["object", "array", "mixed"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_unusable_metadata_is_rejected_without_rolling_back_batch(
    sessions, field, column, limit, bad, shape, legacy
):
    client, verifier, incremental, processor, tasks = await make_client(sessions)
    invalid = notification()
    # Exercise exactly one character past each destination's limit.
    bad = bad[: limit + 1] if isinstance(bad, str) and bad else bad
    invalid["data"][field] = bad
    payload = invalid if shape == "object" else [invalid]
    if shape == "mixed":
        payload.append(notification(minute=2))
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode() + b"\n"
    async with client:
        if legacy:
            digest = hashlib.sha256(raw).hexdigest()
            verified = datetime(2026, 10, 1, tzinfo=UTC)
            async with sessions.begin() as session:
                session.add(
                    WebhookEvent(
                        payload_hash=digest,
                        raw_json=payload,
                        raw_body=raw,
                        signature_verified_at=verified,
                        last_error="preserved legacy error",
                    )
                )
            result = await processor.prepare_existing_batch(digest)
            async with sessions() as session:
                stored = await session.get(WebhookEvent, digest)
                assert stored.last_error == "preserved legacy error"
                assert stored.signature_verified_at.replace(tzinfo=UTC) == verified
            await processor.process_batch(result.batch_hash)
            await processor.prepare_existing_batch(digest)
            await processor.process_batch(digest)
        else:
            for _ in range(2):
                response = await client.post(
                    "/webhooks/google-health",
                    content=raw,
                    headers={
                        "Authorization": "Bearer local-test",
                        "GOOGLE-HEALTH-API-SIGNATURE": "signed",
                    },
                )
                assert response.status_code == 204
                await finish(tasks)
            assert verifier.bodies == [raw, raw]
    async with sessions() as session:
        batch = await session.scalar(select(WebhookEvent))
        element = await session.get(WebhookNotification, payload_hash(invalid))
        members = (
            await session.scalars(select(WebhookBatchItem).order_by(WebhookBatchItem.ordinal))
        ).all()
    assert batch.raw_json == payload and batch.raw_body == raw
    assert batch.element_count == len(members) == (2 if shape == "mixed" else 1)
    items = payload if isinstance(payload, list) else [payload]
    assert [(m.batch_hash, m.ordinal, m.element_hash) for m in members] == [
        (batch.payload_hash, n, payload_hash(item)) for n, item in enumerate(items, 1)
    ]
    assert batch.failure_count == 1 and batch.processed_at is not None
    assert getattr(batch, column) is None and getattr(element, column) is None
    assert element.raw_json == invalid and element.raw_json["data"][field] == bad
    assert element.status == "rejected" and not element.is_valid
    reason = (
        f"must be at most {limit} characters"
        if isinstance(bad, str) and bad
        else "must be a non-empty string"
    )
    assert element.validation_error == f"notification {field} {reason}"
    assert element.attempt_count == element.sync_attempt_count == element.sync_success_count == 0
    assert len(incremental.calls) == (1 if shape == "mixed" else 0)


async def test_metadata_column_boundaries_are_accepted(sessions):
    client, _, incremental, processor, tasks = await make_client(sessions)
    data_type, user, operation = "ğ" * 128, "ü" * 128, "İ" * 32
    incremental.specs = {data_type: REGISTRY["steps"]}
    processor.supported_data_types = {data_type}
    async with sessions.begin() as session:
        token = await session.get(OAuthToken, "me")
        token.health_user_id = user
    item = notification(data_type, user)
    item["data"]["operation"] = operation
    async with client:
        response, _ = await post_signed(client, item)
        await finish(tasks)
    assert response.status_code == 204
    async with sessions() as session:
        for model in (WebhookEvent, WebhookNotification):
            row = await session.scalar(select(model))
            assert (row.data_type, row.health_user_id, row.operation) == (
                data_type,
                user,
                operation,
            )
        assert row.is_valid and row.status == "completed"
    assert [call[0] for call in incremental.calls] == [data_type]
