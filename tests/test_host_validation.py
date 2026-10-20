from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app import main
from app.core.config import Settings
from app.google_health.webhook import WebhookVerificationError


@pytest.fixture
def application(monkeypatch):
    settings = Settings(
        _env_file=None,
        codex_bridge_token=None,
        google_health_webhook_endpoint_uri="https://hooks.example/webhooks/google-health",
        google_health_webhook_authorization="Bearer local-test",
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "build_runtime", lambda *_: SimpleNamespace())
    application = main.create_app()

    @application.get("/host-probe")
    async def probe():
        return {"reached": True}

    return application


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST:8000",
        "127.0.0.1:5174",
        "[::1]:8000",
        # Own cookie jar, apart from other localhost servers (see DEPLOYMENT.md).
        "healthos.localhost:8000",
        "HealthOS.localhost",
    ],
)
async def test_local_http_control(application, host):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://localhost"
    ) as client:
        response = await client.get("/host-probe", headers={"Host": host})
    assert response.status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "attacker.example:8000",
        "localhost.attacker.example",
        "healthos.localhost.attacker.example",
        "other.localhost:8000",
        "localhost@attacker.example",
        "localhost:8000@attacker.example",
        "localhost:8000:evil",
        "%6cocalhost",
        "localhost:65536",
        "localhost:",
        "[::1]evil",
        "0.0.0.0",
        "2130706433",
        "127.1",
        "0x7f000001",
        "attacker@localhost",
        "localhost,attacker.example",
        " localhost",
        "localhost/",
        "localhost\\attacker.example",
        "localhost?attacker.example",
        "localhost#attacker.example",
        "",
    ],
)
async def test_untrusted_http_host_rejected(application, host):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://localhost"
    ) as client:
        response = await client.get("/host-probe", headers={"Host": host})
    assert response.status_code == 400


async def test_duplicate_host_rejected(application):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://localhost"
    ) as client:
        response = await client.get(
            "/host-probe", headers=[("Host", "localhost"), ("Host", "attacker.example")]
        )
    assert response.status_code == 400


@pytest.mark.parametrize("host", ["attacker.example:8000", "hooks.example"])
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/dashboard"),
        ("GET", "/api/dashboard/export.csv"),
        ("POST", "/api/dashboard/measurements"),
        ("POST", "/sync/all"),
        ("POST", "/backfill/start"),
    ],
)
async def test_private_routes_reject_nonlocal_hosts_before_dependencies(
    application, host, method, path
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://localhost"
    ) as client:
        response = await client.request(method, path, headers={"Host": host})
    assert response.status_code == 400


async def test_missing_host_and_forwarded_headers_do_not_grant_access(application):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://localhost"
    ) as client:
        request = client.build_request("GET", "/host-probe")
        del request.headers["host"]
        assert (await client.send(request)).status_code == 400
        response = await client.get(
            "/host-probe",
            headers={
                "Host": "attacker.example",
                "X-Forwarded-Host": "localhost",
                "Forwarded": "host=localhost",
            },
        )
        assert response.status_code == 400


async def websocket_messages(application, host, origin):
    messages = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        messages.append(message)

    await application(
        {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "path": "/api/codex/chat",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", host.encode()), (b"origin", origin.encode())],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
            "subprotocols": [],
        },
        receive,
        send,
    )
    return messages


@pytest.mark.parametrize("host", ["attacker.example:8000", "hooks.example"])
async def test_matching_attacker_origin_cannot_open_chat(application, host):
    messages = await websocket_messages(application, host, f"http://{host}")
    assert messages == [{"type": "websocket.close", "code": 1008, "reason": "Invalid host"}]


async def test_local_chat_still_checks_origin(application):
    messages = await websocket_messages(application, "localhost:8000", "http://localhost:8000")
    assert messages[0]["type"] == "websocket.accept"
    rejected = await websocket_messages(application, "localhost:8000", "http://attacker.example")
    assert rejected[0]["type"] == "websocket.close"
    assert rejected[0]["code"] == 1008


async def test_public_host_only_allows_authenticated_webhook_post(application):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="https://hooks.example"
    ) as client:
        authorized = await client.post(
            "/webhooks/google-health",
            json={"type": "verification"},
            headers={"Authorization": "Bearer local-test"},
        )
        unauthorized = await client.post("/webhooks/google-health", json={"type": "verification"})
        private = await client.get("/host-probe")
        wrong_method = await client.get("/webhooks/google-health")
        redirect = await client.post("/webhooks/google-health/", json={"type": "verification"})
    assert authorized.status_code == 201
    assert unauthorized.status_code == 401
    assert private.status_code == wrong_method.status_code == redirect.status_code == 400


async def test_public_webhook_keeps_signature_validation(application):
    runtime = application.state.runtime
    runtime.webhook_verifier = SimpleNamespace(verify=AsyncMock())
    runtime.webhook_processor = SimpleNamespace(
        ingest_verified=AsyncMock(
            return_value=SimpleNamespace(
                batch_hash="test", element_count=1, inserted=True, should_process=False
            )
        )
    )
    headers = {
        "Authorization": "Bearer local-test",
        "GOOGLE-HEALTH-API-SIGNATURE": "signed",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="https://hooks.example"
    ) as client:
        response = await client.post("/webhooks/google-health", json={"data": {}}, headers=headers)
        assert response.status_code == 204
        runtime.webhook_verifier.verify.assert_awaited_once()
        runtime.webhook_processor.ingest_verified.assert_awaited_once()
        runtime.webhook_verifier.verify.side_effect = WebhookVerificationError("Invalid signature")
        invalid = await client.post("/webhooks/google-health", json={"data": {}}, headers=headers)
        assert invalid.status_code == 401
        assert runtime.webhook_processor.ingest_verified.await_count == 1
