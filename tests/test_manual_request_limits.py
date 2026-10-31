"""Synthetic ASGI streams reach the real manual routes; all stored values are fixtures."""

import asyncio
import json
import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Request
from sqlalchemy import select

import app.main as main
from app.core.config import Settings
from app.core.request_limits import WriteLimits
from app.db.session import get_session
from app.models.manual import ManualMeasurement, ManualWorkout
from app.models.manual_flags import ManualMeasurementFlagEvent

PREFIX = "/api/dashboard"
IDENTIFIER = uuid.UUID(int=1)
TARGETS = [
    "/measurements",
    "/workouts",
    f"/measurements/{IDENTIFIER}/flag",
    f"/measurements/{IDENTIFIER}/unflag",
]
PAYLOADS = [
    {"metric": "weight", "value": "80", "measured_at": "2026-01-01T10:00:00Z"},
    {
        "exercise_type": "Yürüyüş",
        "start_time": "2026-01-01T10:00:00Z",
        "end_time": "2026-01-01T11:00:00Z",
    },
    {"flag": "test", "reason": "deneme"},
    {"flag": "test", "reason": "deneme"},
]


@pytest.fixture
async def application(sessions, monkeypatch):
    config = Settings(_env_file=None, google_health_webhook_authorization="Bearer local-test")
    runtime = SimpleNamespace(webhook_status=SimpleNamespace(record=AsyncMock()))
    monkeypatch.setattr(main, "get_settings", lambda: config)
    monkeypatch.setattr(main, "build_runtime", lambda *args: runtime)
    app = main.create_app()
    app.state.clock = SimpleNamespace(now=1000.0)
    app.state.session_requests = []
    app.state.write_limits = WriteLimits(config, lambda: app.state.clock.now)

    async def session():
        app.state.session_requests.append(True)
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session
    async with sessions.begin() as session:
        session.add(
            ManualMeasurement(
                id=IDENTIFIER,
                metric="weight",
                value=80,
                measured_at=datetime.fromisoformat("2026-01-01T10:00:00Z"),
            )
        )
    return app


async def post(application, path, chunks, headers=(), address=("localhost", 123)):
    read = []

    async def stream():
        for chunk in chunks:
            read.append(len(chunk))
            yield chunk

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application, client=address), base_url="http://localhost"
    ) as client:
        response = await client.post(
            path,
            content=stream(),
            headers=[("X-HealthOS-Request", "1"), ("Content-Type", "application/json"), *headers],
        )
    return response, read


async def snapshot(sessions):
    async with sessions() as session:
        return [
            list((await session.execute(select(table))).scalars())
            for table in (ManualMeasurement, ManualWorkout, ManualMeasurementFlagEvent)
        ]


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("length,reads", [(None, [2048, 2049]), ("1", [2048, 2049]), ("4097", [])])
async def test_every_manual_endpoint_stops_at_limit(
    application, sessions, monkeypatch, target, length, reads
):
    before = [len(rows) for rows in await snapshot(sessions)]
    monkeypatch.setattr(Request, "json", AsyncMock(side_effect=AssertionError("JSON parsed")))
    headers = [] if length is None else [("Content-Length", length)]
    response, read = await post(
        application, PREFIX + target, [b"x" * 2048, b"x" * 2049, b"unread"], headers
    )
    assert response.status_code == 413
    assert read == reads
    assert application.state.session_requests == []
    assert [len(rows) for rows in await snapshot(sessions)] == before


@pytest.mark.parametrize("target,payload", zip(TARGETS, PAYLOADS, strict=True))
@pytest.mark.parametrize("length", [None, "1", "4096"])
async def test_exact_limit_keeps_existing_success(application, sessions, target, payload, length):
    body = json.dumps(payload, ensure_ascii=False).encode()
    body += b" " * (4096 - len(body))
    response, read = await post(
        application,
        PREFIX + target,
        [body[:17], body[17:]],
        [] if length is None else [("Content-Length", length)],
    )
    assert response.status_code == (200 if target.endswith("/unflag") else 201)
    measurements, workouts, events = await snapshot(sessions)
    assert len(measurements) == (2 if target == "/measurements" else 1)
    assert len(workouts) == int(target == "/workouts")
    assert len(events) == int(target.endswith("/flag"))
    assert measurements[0].value == 80


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("lengths", [["-1"], [""], ["1, 1"], ["+2"], ["9" * 21], ["2", "2"]])
async def test_manual_malformed_lengths_never_read_or_parse(
    application, monkeypatch, target, lengths
):
    monkeypatch.setattr(Request, "json", AsyncMock(side_effect=AssertionError("JSON parsed")))
    response, read = await post(
        application, PREFIX + target, [b"{}"], [("Content-Length", n) for n in lengths]
    )
    assert response.status_code == 400
    assert read == application.state.session_requests == []


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("body", [b"{", b'{"flag":"unknown","metric":"unknown"}'])
async def test_normal_validation_remains_422(application, target, body):
    response, _ = await post(application, PREFIX + target, [body])
    assert response.status_code == 422


async def test_manual_budget_shared_by_all_routes_and_refills(application, sessions, monkeypatch):
    application.state.write_limits.manual.capacity = 4
    for target, payload in zip(TARGETS, PAYLOADS, strict=True):
        response, _ = await post(application, PREFIX + target, [json.dumps(payload).encode()])
        assert response.status_code < 300
    before = [len(rows) for rows in await snapshot(sessions)]
    application.state.session_requests.clear()
    original_json = Request.json
    monkeypatch.setattr(Request, "json", AsyncMock(side_effect=AssertionError("JSON parsed")))
    for index in range(8):
        response, read = await post(
            application,
            f"{PREFIX}/measurements/{uuid.UUID(int=index)}/flag",
            [b"unread"],
            [
                ("X-Forwarded-For", str(index)),
                ("Forwarded", f"for={index}"),
                ("X-Request-ID", str(uuid.uuid4())),
            ],
            address=(f"client-{index}", index),
        )
        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) == 60
        assert read == []
    assert application.state.session_requests == []
    assert [len(rows) for rows in await snapshot(sessions)] == before
    monkeypatch.setattr(Request, "json", original_json)
    application.state.clock.now += 60
    response, _ = await post(application, PREFIX + TARGETS[2], [b"{}"])
    assert response.status_code == 201


async def test_budgets_are_separate_and_unrelated_routes_do_not_consume(application):
    application.state.write_limits.manual.capacity = 1
    application.state.write_limits.webhook.capacity = 1
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://localhost"
    ) as client:
        for method, path in [
            ("GET", PREFIX + "/measurements"),
            ("GET", "/health/live"),
            ("GET", "/assets/missing"),
            ("GET", "/api/codex/chat"),
        ]:
            await client.request(method, path)
    for target in [PREFIX + TARGETS[2], "/webhooks/google-health"]:
        headers = [("Authorization", "Bearer local-test")] if target.startswith("/webhooks") else []
        body = b'{"type":"verification"}' if headers else b"{}"
        first, _ = await post(application, target, [body], headers)
        second, read = await post(application, target, [body], headers)
        assert first.status_code == 201
        assert second.status_code == 429 and read == []
    application.state.clock.now += 59.9
    response, _ = await post(application, PREFIX + TARGETS[2], [b"{}"])
    assert int(response.headers["Retry-After"]) == 1
    application.state.clock.now += 0.1
    for target, body in [
        (PREFIX + TARGETS[2], b"{}"),
        ("/webhooks/google-health", b'{"type":"verification"}'),
    ]:
        response, _ = await post(
            application, target, [body], [("Authorization", "Bearer local-test")]
        )
        assert response.status_code < 300


@pytest.mark.parametrize("path", [PREFIX + TARGETS[2], "/webhooks/google-health"])
async def test_concurrent_requests_cannot_overspend(application, path):
    application.state.write_limits.manual.capacity = 3
    application.state.write_limits.webhook.capacity = 3
    headers = [("Authorization", "Bearer local-test")]
    body = b'{"type":"verification"}' if path.startswith("/webhooks") else b"{}"
    results = await asyncio.gather(*(post(application, path, [body], headers) for _ in range(20)))
    assert sum(r.status_code < 300 for r, _ in results) == 3
    assert (
        sum(
            r.status_code == 429 and int(r.headers["Retry-After"]) > 0 and not read
            for r, read in results
        )
        == 17
    )


@pytest.mark.parametrize(
    "path,body",
    [(PREFIX + TARGETS[2], b"{}"), ("/webhooks/google-health", b'{"type":"verification"}')],
)
async def test_application_instances_have_independent_budgets(application, path, body):
    fresh = main.create_app()
    fresh.dependency_overrides = application.dependency_overrides.copy()
    headers = [("Authorization", "Bearer local-test")]
    for app in (application, fresh):
        app.state.write_limits.manual.capacity = 1
        app.state.write_limits.webhook.capacity = 1
        first, _ = await post(app, path, [body], headers)
        rejected, read = await post(app, path, [body], headers)
        assert first.status_code in {200, 201}
        assert rejected.status_code == 429 and read == []


def test_manual_openapi_preserves_body_and_csrf_contract(application):
    paths = application.openapi()["paths"]
    for path, model, required in [
        ("/measurements", "MeasurementInput", True),
        ("/workouts", "WorkoutInput", True),
        ("/measurements/{measurement_id}/flag", "FlagInput", False),
        ("/measurements/{measurement_id}/unflag", "FlagInput", False),
    ]:
        operation = paths[PREFIX + path]["post"]
        body = operation["requestBody"]
        assert body.get("required", False) == required
        schema = body["content"]["application/json"]["schema"]
        assert {"$ref": "#/components/schemas/" + model} == (
            schema if required else schema["anyOf"][0]
        )
        assert "422" in operation["responses"]
        assert any(
            p["name"] == "X-HealthOS-Request" and p["in"] == "header"
            for p in operation["parameters"]
        )


async def test_json_syntax_error_still_precedes_csrf_check(application):
    response, _ = await post(
        application, PREFIX + TARGETS[0], [b"{"], [("X-HealthOS-Request", "bad")]
    )
    assert response.status_code == 422
