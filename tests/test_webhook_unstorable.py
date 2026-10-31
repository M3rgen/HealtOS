"""Signed bodies that jsonb cannot hold are archived byte for byte, never dropped.

PostgreSQL jsonb refuses U+0000, unpaired surrogates and non-finite numbers that
Python's json module accepts. Such a batch keeps its exact bytes in raw_body with
raw_json NULL; each such element is rejected with raw_json NULL and no derived columns,
while its clean siblings are processed. tests/integration/test_pg_webhook_unstorable.py
runs the same scenarios against PostgreSQL, where the old code lost the whole batch.
"""

import hashlib
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, null, select, text
from sqlalchemy.exc import DBAPIError
from test_webhook_route import finish, make_client, notification

from app.models.state import WebhookBatchItem, WebhookEvent, WebhookNotification
from app.parsers.common import payload_hash
from app.services.raw_history import verify_guards
from app.services.webhook_metadata import UNREPRESENTABLE

KINDS = ["nul_value", "nul_key", "nul_data_type", "surrogate", "nan", "overflow"]
REFUSED = "keeps every record"


def unstorable(kind: str) -> dict:
    item = notification()
    data = item["data"]
    if kind == "nul_value":
        data["operation"] = "UP\x00SERT"
    elif kind == "nul_key":
        data["note\x00"] = 1
    elif kind == "nul_data_type":
        data["dataType"] = "steps\x00"
    elif kind == "surrogate":
        data["note"] = "\ud800"
    else:
        data["value"] = float("nan" if kind == "nan" else "inf")
    return item


def body(payload, kind: str = "", indent: int | None = None) -> bytes:
    raw = json.dumps(payload, indent=indent).encode()
    # Python reads 1e400 as inf; json.dumps would write it back as Infinity.
    return raw.replace(b"Infinity", b"1e400") if kind == "overflow" else raw


async def post(client, raw: bytes):
    return await client.post(
        "/webhooks/google-health",
        content=raw,
        headers={"Authorization": "Bearer local-test", "GOOGLE-HEALTH-API-SIGNATURE": "signed"},
    )


async def rows(sessions):
    async with sessions() as session:
        batches = (await session.scalars(select(WebhookEvent))).all()
        elements = (await session.scalars(select(WebhookNotification))).all()
        items = (
            await session.scalars(
                select(WebhookBatchItem).order_by(
                    WebhookBatchItem.batch_hash, WebhookBatchItem.ordinal
                )
            )
        ).all()
    return batches, elements, items


async def snapshot(sessions):
    async with sessions() as session:
        return [
            (await session.execute(text(f"SELECT {columns} FROM {table} ORDER BY 1"))).all()
            for table, columns in (
                ("webhook_events", "payload_hash, raw_json, raw_body, created_at"),
                ("webhook_notifications", "element_hash, raw_json, is_valid, created_at"),
            )
        ]


def assert_rejected(element) -> None:
    assert element.raw_json is None and not element.is_valid
    assert element.status == "rejected" and element.validation_error == UNREPRESENTABLE
    assert (element.data_type, element.health_user_id, element.operation) == (None, None, None)
    assert element.intervals is None and element.attempt_count == 0


@pytest.mark.parametrize("kind", KINDS)
async def test_signed_body_is_archived_verbatim_without_raw_json(sessions, kind):
    client, verifier, incremental, _, tasks = await make_client(sessions)
    raw = body(unstorable(kind), kind)
    async with client:
        response = await post(client, raw)
        await finish(tasks)
    batches, elements, items = await rows(sessions)
    assert response.status_code == 204 and verifier.bodies == [raw]
    [batch], [element], [item] = batches, elements, items
    assert batch.payload_hash == hashlib.sha256(raw).hexdigest()
    assert batch.raw_body == raw and batch.raw_json is None
    # Only clean strings are projected; the element itself keeps nothing derived.
    assert batch.data_type == (None if kind == "nul_data_type" else "steps")
    assert batch.operation == (None if kind == "nul_value" else "UPSERT")
    assert batch.failure_count == batch.element_count == 1 and batch.processed_at is not None
    assert batch.last_error == UNREPRESENTABLE
    assert (item.batch_hash, item.ordinal, item.element_hash) == (
        batch.payload_hash,
        1,
        element.element_hash,
    )
    assert_rejected(element)
    assert incremental.calls == []


async def test_clean_sibling_is_processed_and_redeliveries_deduplicate(sessions):
    client, verifier, incremental, _, tasks = await make_client(sessions)
    clean = notification(minute=2)
    payload = [unstorable("nul_value"), clean]
    raw, respelled = body(payload), body(payload, indent=2)
    async with client:
        for delivery in (raw, raw, respelled):
            assert (await post(client, delivery)).status_code == 204
            await finish(tasks)
    assert verifier.bodies == [raw, raw, respelled]
    batches, elements, items = await rows(sessions)
    # The same bytes are one batch; other bytes are another batch of the same elements.
    assert sorted(batch.raw_body for batch in batches) == sorted([raw, respelled])
    assert all(batch.raw_json is None and batch.failure_count == 1 for batch in batches)
    assert all(batch.processed_at is not None for batch in batches)
    assert len(elements) == 2 and len(items) == 4
    by_hash = {element.element_hash: element for element in elements}
    good = by_hash.pop(payload_hash(clean))
    assert good.raw_json == clean and good.status == "completed" and good.data_type == "steps"
    assert_rejected(next(iter(by_hash.values())))
    assert len(incremental.calls) == 1


async def test_body_only_batch_is_prepared_from_its_raw_body(sessions):
    _, _, incremental, processor, _ = await make_client(sessions)
    clean = notification(minute=4)
    raw = body([unstorable("surrogate"), clean])
    digest = hashlib.sha256(raw).hexdigest()
    async with sessions.begin() as session:
        session.add(
            WebhookEvent(
                payload_hash=digest,
                raw_json=null(),
                raw_body=raw,
                signature_verified_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
        )
    result = await processor.process_pending(recover_running=False)
    assert (result.completed_elements, result.rejected_elements) == (1, 1)
    batches, elements, items = await rows(sessions)
    assert [(batch.raw_json, batch.raw_body) for batch in batches] == [(None, raw)]
    assert [item.element_hash for item in items][1] == payload_hash(clean)
    assert len(elements) == 2 and len(incremental.calls) == 1


async def test_guards_freeze_null_payloads_and_their_bodies(sessions):
    client, _, _, _, tasks = await make_client(sessions)
    async with client:
        for payload in ([unstorable("nul_key"), notification(minute=6)], notification(minute=8)):
            assert (await post(client, body(payload))).status_code == 204
            await finish(tasks)
    before = await snapshot(sessions)
    nulls = "WHERE raw_json IS NULL"
    refused = {
        f"UPDATE webhook_events SET raw_json = '{{}}' {nulls}": REFUSED,
        "UPDATE webhook_events SET raw_json = NULL WHERE raw_json IS NOT NULL": REFUSED,
        f"UPDATE webhook_events SET raw_body = NULL {nulls}": REFUSED,
        f"UPDATE webhook_notifications SET raw_json = '{{}}' {nulls}": REFUSED,
        "UPDATE webhook_notifications SET raw_json = NULL WHERE raw_json IS NOT NULL": REFUSED,
        f"DELETE FROM webhook_events {nulls}": REFUSED,
        f"DELETE FROM webhook_notifications {nulls}": REFUSED,
        f"UPDATE webhook_notifications SET is_valid = true {nulls}": "raw_or_rejected",
        "INSERT INTO webhook_events (payload_hash, element_count, failure_count) "
        "VALUES ('e', 0, 0)": "raw_kept",
    }
    for statement, message in refused.items():
        with pytest.raises(DBAPIError, match=message):
            async with sessions.begin() as session:
                await session.execute(text(statement))
    assert await snapshot(sessions) == before
    async with sessions.begin() as session:
        for table in ("webhook_events", "webhook_notifications"):
            await session.execute(text(f"UPDATE {table} SET last_error = 'kept' {nulls}"))
    async with sessions() as session:
        assert await session.scalar(
            select(func.count()).where(WebhookNotification.last_error == "kept")
        )
        assert await verify_guards(session) == []
