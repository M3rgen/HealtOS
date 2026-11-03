"""Real synthetic Tink/ECDSA signatures through HTTP, archive and diagnostics."""

import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from operator import attrgetter
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from pythonjsonlogger.json import JsonFormatter
from raw_response_records import body as google_body
from raw_response_records import client as google_client
from raw_response_records import steps, stored
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sync_ownership_records import service, snapshot
from test_webhook_route import Incremental, notification
from test_webhook_status import START, configured

from app.api import webhooks
from app.core.auth import SessionStore, UITokenMiddleware
from app.core.host_validation import HostValidationMiddleware
from app.core.request_limits import WriteLimits
from app.google_health.client import GoogleHealthError
from app.google_health.webhook import WebhookSignatureVerifier
from app.models.state import OAuthToken, WebhookEvent, WebhookNotification
from app.models.webhook_status import WebhookStatusEvent
from app.services import webhook_processing
from app.services.webhook_processing import SIGNED_REPLAY_MAX_AGE, WebhookBatchProcessor
from app.services.webhook_status import WebhookStatusService


@pytest.fixture
async def receiver(sessions, monkeypatch):
    current = SimpleNamespace(now=START, key_requests=0, key_response="ok")

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return current.now

    monkeypatch.setattr(webhook_processing, "datetime", Frozen)
    settings = configured()
    status = WebhookStatusService(settings, sessions, lambda: current.now)
    await status.start()
    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.public_key().public_numbers()
    serialized = b"\x08\x00\x1a\x20" + numbers.x.to_bytes(32) + b"\x22\x20" + numbers.y.to_bytes(32)
    keyset = {
        "key": [
            {
                "keyId": 42,
                "status": "ENABLED",
                "keyData": {"value": base64.b64encode(serialized).decode()},
            }
        ]
    }

    def key_server(request):
        current.key_requests += 1
        if current.key_response == "timeout":
            raise httpx.ReadTimeout("synthetic private upstream error", request=request)
        if current.key_response == "503":
            return httpx.Response(503, text="synthetic private upstream error")
        if current.key_response == "malformed":
            return httpx.Response(200, json={"key": ["bad"]})
        if current.key_response == "json":
            return httpx.Response(200, text="not json")
        return httpx.Response(200, json=keyset)

    key_http = httpx.AsyncClient(transport=httpx.MockTransport(key_server))
    verifier = WebhookSignatureVerifier(settings.google_health_webhook_public_key_url, key_http)
    incremental = Incremental()
    processor = WebhookBatchProcessor(sessions, incremental, set(incremental.specs), status)
    async with sessions.begin() as session:
        session.add(
            OAuthToken(subject="me", encrypted_refresh_token="synthetic", health_user_id="user-1")
        )
    tasks = []

    def spawn(coroutine):
        tasks.append(asyncio.create_task(coroutine))

    async def finish():
        try:
            await asyncio.gather(*tasks)
        finally:
            tasks.clear()

    def sign(body):
        signed = b"\x01\x00\x00\x00\x2a" + key.sign(body, ec.ECDSA(hashes.SHA256()))
        return base64.b64encode(signed).decode()

    app = FastAPI()
    app.state.settings = settings
    app.state.runtime = SimpleNamespace(
        webhook_verifier=verifier, webhook_processor=processor, webhook_status=status, spawn=spawn
    )
    app.add_middleware(UITokenMiddleware, token="synthetic-ui", sessions=SessionStore())
    app.add_middleware(
        HostValidationMiddleware, webhook_endpoint_uri=settings.google_health_webhook_endpoint_uri
    )
    app.state.write_limits = WriteLimits(app.state.settings)
    app.include_router(webhooks.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:

        async def post(body=None, signature=True, authorization="Bearer synthetic-test"):
            body = body if body is not None else json.dumps(notification()).encode()
            headers = {}
            if authorization is not None:
                headers["Authorization"] = authorization
            if signature is not None:
                headers["GOOGLE-HEALTH-API-SIGNATURE"] = (
                    sign(body) if signature is True else signature
                )
            return await client.post("/webhooks/google-health", content=body, headers=headers)

        yield SimpleNamespace(
            client=client,
            post=post,
            sign=sign,
            status=status,
            current=current,
            processor=processor,
            finish=finish,
            incremental=incremental,
            app=app,
        )
        await finish()
    await key_http.aclose()


async def test_status_requires_ui_auth_and_public_host_has_only_post(receiver, sessions):
    client = receiver.client
    assert (await client.get("/api/webhooks/status")).status_code == 401
    before = (await receiver.status.snapshot())["counters"]
    for _ in range(2):
        response = await client.get(
            "/api/webhooks/status", headers={"Authorization": "Bearer synthetic-ui"}
        )
        assert response.status_code == 200
        assert response.json()["subscription"]["renewal_status"] == "unknown"
    assert receiver.current.key_requests == 0
    assert (await receiver.status.snapshot())["counters"] == before
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(WebhookStatusEvent)) == 2
    public = "https://hooks.example"
    for method, path in [
        ("GET", "/api/webhooks/status"),
        ("POST", "/api/webhooks/status"),
        ("GET", "/webhooks/google-health"),
        ("GET", "/health"),
    ]:
        blocked = await client.request(
            method, public + path, headers={"Authorization": "Bearer synthetic-ui"}
        )
        assert blocked.status_code == 400
    challenge = await client.post(
        public + "/webhooks/google-health",
        json={"type": "verification"},
        headers={"Authorization": "Bearer synthetic-test"},
    )
    assert challenge.status_code == 201


async def test_authorization_missing_malformed_invalid_and_challenge_are_separate(receiver):
    assert (await receiver.post(authorization=None)).status_code == 401
    assert (await receiver.post(authorization="Bearer wrong")).status_code == 401
    assert (await receiver.post(signature=None)).status_code == 401
    assert (await receiver.post(signature="%%%")).status_code == 401
    assert (
        await receiver.post(signature=base64.b64encode(b"bad prefix").decode())
    ).status_code == 401
    bad_signature = receiver.sign(b"different raw body")
    assert (await receiver.post(signature=bad_signature)).status_code == 401
    assert (await receiver.post(b'{"type":"verification"}', signature=None)).status_code == 201
    snapshot = await receiver.status.snapshot()
    reasons = snapshot["counters"]["reasons"]
    # Both rejections fall inside one sampling minute: one row, one counted in memory.
    assert reasons["authorization_rejected"] == 1
    assert snapshot["process_local"]["unpersisted"]["authorization_rejected"] == 1
    assert reasons["missing_signature"] == 1
    assert reasons["malformed_signature"] == 2
    assert reasons["invalid_signature"] == 1
    assert reasons["challenge"] == 1
    assert snapshot["counters"]["invalid"] == 4
    assert snapshot["counters"]["valid"] == snapshot["counters"]["expired"] == 0
    assert snapshot["last_notification_at"] is None
    assert snapshot["last_error"] == {
        "code": "invalid_signature",
        "stage": "validation",
        "at": START,
    }
    assert receiver.incremental.calls == []


@pytest.mark.parametrize("failure", ["timeout", "503", "malformed", "json"])
async def test_key_server_failure_is_not_invalid_signature_or_processing(receiver, failure):
    receiver.current.key_response = failure
    # A transient keyset outage is not a rejection: 503 lets Google retry the delivery.
    response = await receiver.post()
    assert response.status_code == 503
    assert response.json() == {"detail": "Webhook verification temporarily unavailable"}
    assert receiver.incremental.calls == []
    snapshot = await receiver.status.snapshot()
    assert snapshot["counters"]["reasons"]["key_download_failed"] == 1
    assert snapshot["counters"]["invalid"] == snapshot["counters"]["valid"] == 0
    assert snapshot["last_error"]["code"] == "key_download_failed"
    assert snapshot["last_error"]["stage"] == "validation"
    assert "private" not in json.dumps(snapshot, default=str)
    assert snapshot["last_notification_at"] is None


async def test_duplicate_and_old_signed_replay_preserve_archive_and_silence(receiver, sessions):
    body = b" [ " + json.dumps(notification(), ensure_ascii=True).encode() + b" ]\n"
    digest = hashlib.sha256(body).hexdigest()
    assert (await receiver.post(body)).status_code == 204
    await receiver.finish()
    receiver.current.now += SIGNED_REPLAY_MAX_AGE
    # Exactly 24h is still accepted by the existing strictly-greater-than policy.
    assert (await receiver.post(body)).status_code == 204
    await receiver.finish()
    last_accepted = receiver.current.now
    receiver.current.now += timedelta(days=8)
    assert (await receiver.post(body)).status_code == 204
    await receiver.finish()
    status = await receiver.status.snapshot()
    counts = status["counters"]
    assert counts["valid"] == 3 and counts["invalid"] == 0 and counts["expired"] == 1
    assert counts["reasons"]["duplicate"] == 1
    assert counts["reasons"]["notification_received"] == 2
    assert "signature has no expiry field" in counts["expired_basis"]
    assert status["last_notification_at"] == last_accepted
    assert status["silence"]["warning"] is True
    assert status["last_error"] is None
    assert len(receiver.incremental.calls) == 1
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(WebhookEvent)) == 1
        batch = await session.get(WebhookEvent, digest)
        assert batch.raw_body == body
        assert batch.raw_json == [notification()]
        assert batch.signature_verified_at.replace(tzinfo=UTC) == START


async def test_processing_failure_and_rejection_do_not_count_as_verification_failures(receiver):
    receiver.incremental.fail_once.add("steps")
    assert (await receiver.post()).status_code == 204
    await receiver.finish()
    first = await receiver.status.snapshot()
    assert first["counters"]["valid"] == 1 and first["counters"]["invalid"] == 0
    assert first["last_error"] == {"code": "processing_failed", "stage": "processing", "at": START}
    receiver.current.now += timedelta(seconds=1)
    assert (
        await receiver.post(json.dumps(notification(user="other-user")).encode())
    ).status_code == 204
    await receiver.finish()
    final = await receiver.status.snapshot()
    assert final["counters"]["valid"] == 2 and final["counters"]["invalid"] == 0
    assert final["counters"]["reasons"]["notification_rejected"] == 1
    assert final["last_notification_at"] == START
    assert final["last_error"] == {
        "code": "notification_rejected",
        "stage": "processing",
        "at": receiver.current.now,
    }
    assert "user" not in json.dumps(final, default=str)


@pytest.mark.parametrize("kind", ["ordinary", "sqlalchemy", "google"])
async def test_processing_errors_are_private_and_retry_preserves_archive(
    receiver, sessions, caplog, kind
):
    marker = "synthetic-private-health-record"
    record = steps(42, name=f"users/me/dataTypes/steps/dataPoints/{marker}")
    failure = {
        "ordinary": RuntimeError(marker),
        "sqlalchemy": OperationalError(
            "INSERT INTO raw_health_data (raw_json) VALUES (:raw_json)",
            {"raw_json": record},
            RuntimeError("synthetic database failure"),
        ),
        "google": GoogleHealthError(f"HTTP 503 {marker}"),
    }[kind]
    summary = type(failure).__name__ + (" (http_503)" if kind == "google" else "")
    wire = google_body(steps(41, name=record["name"]), record, token="resume")
    replies = iter([httpx.Response(200, content=wire), failure, httpx.Response(200, json={})])

    def respond(request):
        reply = next(replies)
        if isinstance(reply, Exception):
            try:
                raise ValueError(f"{marker} context")
            except ValueError as context:
                raise reply from context
        return reply

    google = google_client(respond, sessions)
    receiver.processor.incremental = service(sessions, google)
    body = b" [ " + json.dumps(notification()).encode() + b" ]\n"
    digest = hashlib.sha256(body).hexdigest()
    counts = attrgetter("attempt_count", "sync_attempt_count", "sync_success_count")
    caplog.set_level("INFO")
    async with google.http:
        assert (await receiver.post(body)).status_code == 204
        await receiver.finish()
        async with sessions() as session:
            element = await session.scalar(select(WebhookNotification))
            batch = await session.get(WebhookEvent, digest)
            assert element.status == "failed" and element.processed_at is None
            assert counts(element) == (1, 1, 0)
            assert element.last_error == batch.last_error == summary
            assert batch.failure_count == 1 and batch.processed_at is None
            archive = batch.signature_verified_at, element.element_hash, element.raw_json
        before = await snapshot(sessions)
        responses = [row.body for row in await stored(sessions)]
        assert responses == [wire]
        response = await receiver.client.get(
            "/api/webhooks/status", headers={"Authorization": "Bearer synthetic-ui"}
        )
        assert response.status_code == 200 and marker not in response.text
        assert response.json()["counters"]["reasons"]["processing_failed"] == 1
        assert response.json()["last_error"]["code"] == "processing_failed"
        (log,) = [r for r in caplog.records if getattr(r, "event", "") == "WEBHOOK_ELEMENT_FAILED"]
        assert log.exc_info is log.exc_text is log.stack_info is None
        assert log.args == () and log.getMessage() == "Webhook notification processing failed"
        assert log.error_type == type(failure).__name__
        assert log.error_code == ("http_503" if kind == "google" else None)
        assert log.error_chain == type(failure).__name__ + " < ValueError"
        assert not any(isinstance(value, BaseException) for value in vars(log).values())
        assert marker not in repr([r.__dict__ for r in caplog.records])
        assert marker not in "\n".join(JsonFormatter().format(r) for r in caplog.records)
        result = await receiver.processor.process_pending(recover_running=False)
        assert result.completed_elements == 1 and result.failed_elements == 0
    async with sessions() as session:
        element = await session.scalar(select(WebhookNotification))
        batch = await session.get(WebhookEvent, digest)
        assert element.status == "completed" and element.processed_at is not None
        assert counts(element) == (2, 2, 1)
        assert element.last_error is batch.last_error is None
        assert batch.failure_count == 0 and batch.processed_at is not None
        assert (batch.signature_verified_at, element.element_hash, element.raw_json) == archive
        assert batch.raw_body == body and batch.raw_json == [notification()]
    final = await snapshot(sessions)
    for table in ("raw_health_data", "raw_health_data_versions"):
        assert final[table] == before[table] and before[table]
    after = await stored(sessions)
    assert [row.body for row in after][: len(responses)] == responses
    assert all(row.body_sha256 == hashlib.sha256(row.body).hexdigest() for row in after)
    assert (await receiver.status.snapshot())["counters"]["reasons"]["processing_failed"] == 1


async def test_ingestion_failure_is_separate_and_cannot_reset_silence(receiver):
    receiver.current.now += timedelta(days=8)
    receiver.app.state.runtime.webhook_processor = SimpleNamespace(
        ingest_verified=AsyncMock(side_effect=RuntimeError("synthetic private payload"))
    )
    with pytest.raises(RuntimeError, match="synthetic private"):
        await receiver.post()
    status = await receiver.status.snapshot()
    assert status["counters"]["valid"] == 1
    assert status["counters"]["invalid"] == 0
    assert status["last_error"]["code"] == "ingestion_failed"
    assert status["last_error"]["stage"] == "processing"
    assert status["silence"]["warning"] is True
    assert status["last_notification_at"] is None


async def test_recovery_reports_safe_interruption_and_preserves_existing_error_archive(
    receiver, sessions
):
    assert (await receiver.post()).status_code == 204
    await receiver.finish()
    async with sessions.begin() as session:
        element = await session.scalar(select(WebhookNotification))
        element.status = "running"
        element.updated_at = START
    receiver.current.now += timedelta(days=1)
    assert await receiver.processor.recover_running(receiver.current.now) == 1
    status = await receiver.status.snapshot()
    assert status["last_error"]["code"] == "processing_interrupted"
    assert status["counters"]["reasons"]["processing_interrupted"] == 1


async def test_unavailable_status_is_safe_and_diagnostics_outage_preserves_delivery_codes(
    receiver, monkeypatch
):
    monkeypatch.setattr(
        receiver.status, "snapshot", AsyncMock(side_effect=SQLAlchemyError("private secret"))
    )
    response = await receiver.client.get(
        "/api/webhooks/status", headers={"Authorization": "Bearer synthetic-ui"}
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Webhook status unavailable"}

    # Only direct diagnostic writes fail; the raw archive remains available.
    def unavailable():
        raise SQLAlchemyError("private secret")

    monkeypatch.setattr(receiver.status, "sessions", SimpleNamespace(begin=unavailable))
    assert (await receiver.post(authorization=None)).status_code == 401
    assert (await receiver.post(b'{"type":"verification"}', signature=None)).status_code == 201
    assert (await receiver.post()).status_code == 204
    await receiver.finish()


async def test_non_signature_request_failures_have_their_own_codes(receiver):
    assert (await receiver.post(b"not-json")).status_code == 400
    await receiver.finish()
    receiver.app.state.settings.google_health_webhook_max_body_bytes = 2
    assert (await receiver.post(b"{} ")).status_code == 413
    receiver.app.state.settings.google_health_webhook_authorization = "CHANGE_ME"
    assert (await receiver.post()).status_code == 503
    status = await receiver.status.snapshot()
    for code in ("invalid_json", "body_rejected", "receiver_unconfigured"):
        assert status["counters"]["reasons"][code] == 1
    # Malformed signed JSON is now verified and archived before its controlled 400.
    assert status["counters"]["valid"] == 1 and status["counters"]["invalid"] == 0
    assert status["silence"]["state"] == "unconfigured"


@pytest.mark.parametrize("payload", [[], {}, ["malformed"], notification(user="other-user")])
async def test_signed_rejected_content_does_not_silence_warning(receiver, payload):
    receiver.current.now += timedelta(days=8)
    assert (await receiver.post(json.dumps(payload).encode())).status_code == 204
    await receiver.finish()
    status = await receiver.status.snapshot()
    assert status["counters"]["valid"] == 1
    assert status["counters"]["invalid"] == 0
    assert status["last_notification_at"] is None
    assert status["silence"]["warning"] is True


async def test_unhandled_background_batch_error_is_processing_only(receiver, monkeypatch):
    monkeypatch.setattr(
        receiver.processor,
        "_finalize_batch",
        AsyncMock(side_effect=RuntimeError("synthetic private error")),
    )
    assert (await receiver.post()).status_code == 204
    with pytest.raises(RuntimeError, match="synthetic private"):
        await receiver.finish()
    status = await receiver.status.snapshot()
    assert status["last_error"] == {
        "code": "batch_processing_failed",
        "stage": "processing",
        "at": START,
    }
    assert status["counters"]["valid"] == 1
    assert status["counters"]["invalid"] == 0
    assert status["counters"]["reasons"]["batch_processing_failed"] == 1


@pytest.mark.parametrize(
    "rejection,code", [("rate", 429), ("rate_unauthorized", 429), ("body", 413)]
)
async def test_limits_do_not_advance_verified_delivery_or_start_work(
    receiver, sessions, monkeypatch, rejection, code
):
    receiver.app.state.settings.google_health_webhook_max_body_bytes = 1024
    receiver.app.state.write_limits.webhook.capacity = 120 if rejection == "body" else 1
    receiver.app.state.write_limits.webhook_unauthorized.capacity = 1
    assert (await receiver.post()).status_code == 204
    await receiver.finish()
    if rejection == "rate_unauthorized":
        # The unauthorized budget is separate: spend it with one rejected request first.
        assert (await receiver.post(authorization=None)).status_code == 401
    before = await receiver.status.snapshot()
    key_requests = receiver.current.key_requests
    receiver.current.now += timedelta(hours=1)
    monkeypatch.setattr(
        webhooks, "parse_webhook_body", Mock(side_effect=AssertionError("JSON parsed"))
    )
    monkeypatch.setattr(
        receiver.app.state.runtime.webhook_verifier,
        "verify",
        AsyncMock(side_effect=AssertionError("Signature checked")),
    )
    response = await receiver.post(
        b"x" * 1025,
        signature="signed",
        authorization=None if rejection == "rate_unauthorized" else "Bearer synthetic-test",
    )
    assert response.status_code == code
    if code == 429:
        assert int(response.headers["Retry-After"]) > 0
    after = await receiver.status.snapshot()
    assert after["last_notification_at"] == before["last_notification_at"]
    assert after["counters"]["valid"] == before["counters"]["valid"]
    assert receiver.current.key_requests == key_requests
    assert len(receiver.incremental.calls) == 1
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(WebhookEvent)) == 1
    if code == 429:
        assert after["counters"] == before["counters"]
    else:
        assert after["counters"]["reasons"]["body_rejected"] == 1


async def test_unauthorized_flood_cannot_starve_signed_delivery(receiver):
    limits = receiver.app.state.write_limits
    limits.webhook.capacity = limits.webhook_unauthorized.capacity = 3
    codes = [(await receiver.post(authorization=None)).status_code for _ in range(5)]
    codes.append((await receiver.post(authorization="Bearer wrong")).status_code)
    assert codes == [401, 401, 401, 429, 429, 429]
    assert (await receiver.post()).status_code == 204
    await receiver.finish()
    for _ in range(2):
        assert (await receiver.post(b'{"type":"verification"}', signature=None)).status_code == 201
    assert (await receiver.post(b'{"type":"verification"}', signature=None)).status_code == 429
    response = await receiver.client.get(
        "/api/webhooks/status", headers={"Authorization": "Bearer synthetic-ui"}
    )
    status = response.json()
    assert status["counters"]["reasons"]["authorization_rejected"] == 1
    assert status["counters"]["reasons"]["notification_received"] == 1
    assert status["counters"]["valid"] == 1
    assert status["process_local"]["unpersisted"]["authorization_rejected"] == 2
    assert status["process_local"]["rate_limited"] == {"authorized": 1, "unauthorized": 3}
    assert status["last_notification_at"] is not None


@pytest.mark.parametrize(
    "field,limit", [("dataType", 128), ("healthUserId", 128), ("operation", 32)]
)
@pytest.mark.parametrize("bad", [{}, [], "ğ" * 129])
@pytest.mark.parametrize("shape", ["object", "array", "mixed"])
async def test_signed_bad_metadata_preserves_diagnostics_and_valid_members(
    receiver, sessions, field, limit, bad, shape
):
    invalid = notification()
    invalid["data"][field] = bad[: limit + 1] if isinstance(bad, str) else bad
    payload = invalid if shape == "object" else [invalid]
    if shape == "mixed":
        payload.append(notification(minute=2))
    receiver.current.now += timedelta(days=8)
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode() + b"\n"
    assert (await receiver.post(body)).status_code == 204
    await receiver.finish()
    async with sessions() as session:
        history = (
            await session.execute(
                select(WebhookStatusEvent.__table__).order_by(WebhookStatusEvent.id)
            )
        ).all()
    assert (await receiver.post(body)).status_code == 204
    await receiver.finish()
    status = await receiver.status.snapshot()
    reasons = status["counters"]["reasons"]
    assert reasons["notification_rejected"] == reasons["duplicate"] == 1
    assert reasons.get("notification_received", 0) == (2 if shape == "mixed" else 0)
    assert reasons.get("ingestion_failed", 0) == 0
    assert status["counters"]["valid"] == 2 and status["counters"]["invalid"] == 0
    assert status["last_notification_at"] == (receiver.current.now if shape == "mixed" else None)
    assert status["silence"]["warning"] is (shape != "mixed")
    assert len(receiver.incremental.calls) == (1 if shape == "mixed" else 0)
    async with sessions() as session:
        batch = await session.scalar(select(WebhookEvent))
        assert batch.raw_body == body and batch.raw_json == payload
        after = (
            await session.execute(
                select(WebhookStatusEvent.__table__).order_by(WebhookStatusEvent.id)
            )
        ).all()
    assert after[: len(history)] == history


async def test_database_ingestion_failure_rolls_back_and_propagates(receiver, sessions):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    def fail_notification_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO webhook_notifications"):
            raise OperationalError(statement, parameters, RuntimeError("synthetic database outage"))

    engine = sessions.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", fail_notification_insert)
    try:
        with pytest.raises(OperationalError, match="synthetic database outage"):
            await receiver.post()
    finally:
        event.remove(engine, "before_cursor_execute", fail_notification_insert)
    assert receiver.incremental.calls == []
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(WebhookEvent)) == 0
        assert await session.scalar(select(func.count()).select_from(WebhookNotification)) == 0
    snapshot = await receiver.status.snapshot()
    assert snapshot["counters"]["reasons"]["ingestion_failed"] == 1
    assert snapshot["counters"]["reasons"].get("notification_received", 0) == 0
    assert snapshot["last_notification_at"] is None
