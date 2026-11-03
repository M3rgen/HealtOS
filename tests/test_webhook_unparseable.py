"""Parser failures preserve authenticated bytes without admitting unsigned archives."""

import hashlib
import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import func, null, select
from test_webhook_date_windows import fetching_receiver as fetching_receiver
from test_webhook_route import notification
from test_webhook_status_http import receiver as receiver
from test_webhook_unstorable import rows, snapshot

from app.models.retrieval import RetrievalReceipt
from app.models.state import RawHealthData, SyncState, WebhookEvent
from app.services import webhook_metadata

DEEP = b"[" * 100_000 + b"0" + b"]" * 100_000
NOTICE = notification()
DUPLICATES = [
    json.dumps(NOTICE)
    .replace(
        f"{json.dumps(field)}: {json.dumps(value)}",
        f'{json.dumps(field)}: {json.dumps(value)}, "{key}": {json.dumps(repeated)}',
    )
    .encode()
    for field, value in [
        ("data", NOTICE["data"]),
        *NOTICE["data"].items(),
        *NOTICE["data"]["intervals"][0]["physicalTimeInterval"].items(),
    ]
    for key in (field, f"\\u{ord(field[0]):04x}{field[1:]}")
    for repeated in (value, [] if field == "intervals" else None)
]
CHALLENGE = b'{"type":"verification","t\\u0079pe":"verification"}'
DUPLICATES += [
    CHALLENGE,
    b'{"extra":[{"nested":{"private-key":"private-value","private-key":0}}]}',
    b"[" + json.dumps(NOTICE).encode() + b"," + DUPLICATES[0] + b"]",
]
BODIES = [DEEP, b'{"broken":', b'"\xff"', b"9" * 10_000, *DUPLICATES]


@pytest.mark.parametrize("body", BODIES)
async def test_signed_parser_failure_preserves_archive(fetching_receiver, sessions, body, caplog):
    receiver = fetching_receiver
    caplog.set_level(logging.INFO)
    for _ in range(2):
        response = await receiver.post(body)
        assert response.status_code == 400
        assert response.json() == {"detail": "Invalid JSON payload"}
        await receiver.finish()
        if _ == 0:
            before = await snapshot(sessions)
    assert await snapshot(sessions) == before
    [batch], [element], [item] = await rows(sessions)
    assert batch.payload_hash == hashlib.sha256(body).hexdigest()
    assert batch.raw_body == body and batch.raw_json is None
    assert (batch.data_type, batch.health_user_id, batch.operation) == (None, None, None)
    assert batch.signature_verified_at is not None
    assert batch.element_count == batch.failure_count == 1 and batch.processed_at is not None
    assert element.raw_json is None and not element.is_valid and element.status == "rejected"
    assert (element.data_type, element.health_user_id, element.operation, element.intervals) == (
        None,
        None,
        None,
        None,
    )
    assert element.attempt_count == element.sync_attempt_count == element.sync_success_count == 0
    assert element.validation_error == batch.last_error == webhook_metadata.UNPARSEABLE
    assert (item.batch_hash, item.ordinal, item.element_hash) == (
        batch.payload_hash,
        1,
        element.element_hash,
    )
    status = await receiver.status.snapshot()
    reasons = status["counters"]["reasons"]
    assert reasons["signature_valid"] == reasons["invalid_json"] == 2
    assert reasons["notification_rejected"] == reasons["duplicate"] == 1
    assert reasons.get("ingestion_failed", 0) == 0
    assert status["last_notification_at"] is None and receiver.incremental.calls == []
    async with sessions() as session:
        for model in (RetrievalReceipt, SyncState, RawHealthData):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    assert not any(
        key in repr([record.__dict__ for record in caplog.records])
        for key in ("dataType", "intervals", "startTime", "endTime", "private-key", "private-value")
    )


@pytest.mark.parametrize("payload", [NOTICE, [NOTICE, notification(minute=2)]])
async def test_distinct_objects_may_share_keys(receiver, sessions, payload):
    assert (await receiver.post(json.dumps(payload).encode())).status_code == 204
    await receiver.finish()
    [batch], elements, _ = await rows(sessions)
    assert batch.raw_json == payload and batch.failure_count == 0
    assert all(element.is_valid and element.status == "completed" for element in elements)


@pytest.mark.parametrize("body", [DEEP, DUPLICATES[0], CHALLENGE])
@pytest.mark.parametrize(
    "signature,authorization,reason",
    [
        (None, "Bearer synthetic-test", "missing_signature"),
        ("%%%", "Bearer synthetic-test", "malformed_signature"),
        ("invalid", "Bearer synthetic-test", "invalid_signature"),
        (True, None, "authorization_rejected"),
        (True, "Bearer wrong", "authorization_rejected"),
    ],
)
async def test_untrusted_body(receiver, sessions, signature, authorization, reason, body):
    if signature == "invalid":
        signature = receiver.sign(b"different body")
    assert (
        await receiver.post(body, signature=signature, authorization=authorization)
    ).status_code == 401
    assert await rows(sessions) == ([], [], [])
    status = await receiver.status.snapshot()
    # A rejected signature is the only verdict: the unsigned body never counts as invalid_json.
    assert {key: n for key, n in status["counters"]["reasons"].items() if n} == {reason: 1}
    assert status["counters"]["valid"] == 0


@pytest.mark.parametrize("body", [DEEP, *DUPLICATES])
async def test_body_only_replay_and_distinct_deliveries(receiver, sessions, body):
    digest = hashlib.sha256(body).hexdigest()
    async with sessions.begin() as session:
        session.add(
            WebhookEvent(
                payload_hash=digest,
                raw_json=null(),
                raw_body=body,
                signature_verified_at=receiver.current.now,
            )
        )
    before = await snapshot(sessions)
    result = await receiver.processor.process_pending(recover_running=False)
    assert result.rejected_elements == 1 and result.completed_elements == 0
    assert (await snapshot(sessions))[0] == before[0]
    before = await snapshot(sessions)
    await receiver.processor.prepare_existing_batch(digest)
    await receiver.processor.process_pending(recover_running=False)
    assert await snapshot(sessions) == before
    for delivery in (body, body + b" ", body + b" "):
        assert (await receiver.post(delivery)).status_code == 400
        await receiver.finish()
    await receiver.processor.process_pending(recover_running=False)
    await receiver.finish()
    batches, elements, items = await rows(sessions)
    assert {batch.raw_body for batch in batches} == {body, body + b" "}
    assert len(elements) == len(items) == 2
    assert all(not element.is_valid and element.status == "rejected" for element in elements)
    assert len({element.element_hash for element in elements}) == 2
    assert receiver.incremental.calls == []


@pytest.mark.parametrize("error", [OverflowError, MemoryError])
async def test_other_parser_failures(receiver, sessions, monkeypatch, error):
    monkeypatch.setattr(webhook_metadata, "json", SimpleNamespace(loads=Mock(side_effect=error)))
    assert (await receiver.post(b"{} ")).status_code == 400
    await receiver.finish()
    [batch], [element], _ = await rows(sessions)
    assert batch.raw_body == b"{} " and batch.raw_json is None
    assert element.status == "rejected" and not element.is_valid
