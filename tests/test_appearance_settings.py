from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from app import main
from app.core.config import Settings
from app.db.session import get_session
from app.main import create_app
from app.models.settings import AppearanceSettings

URL = "/api/settings/appearance"
JOB = {"X-HealthOS-Request": "1"}
TOKEN = "t" * 20 + "-test-ui-token-0123456789"


@pytest.fixture
def application(sessions):
    application = create_app()

    async def override_session():
        async with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    return application


def client(application, **kwargs):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://localhost", **kwargs
    )


async def test_default_is_empty_selection(application):
    async with client(application) as http:
        response = await http.get(URL)
    assert response.status_code == 200
    assert response.json() == {"selection": {}, "updated_at": None}


async def test_put_then_get_roundtrip_last_write_wins(application, sessions):
    async with client(application, headers=JOB) as http:
        first = await http.put(URL, json={"logo": "dial", "umay": "umay-alt"})
        assert first.status_code == 200
        assert first.json()["selection"] == {"logo": "dial", "umay": "umay-alt"}
        second = await http.put(URL, json={"logo": "yurt", "background": "upload"})
        assert second.status_code == 200
        stored = (await http.get(URL)).json()
    # A full replace, not a merge: `umay` from the first write is gone.
    assert stored["selection"] == {"logo": "yurt", "background": "upload"}
    assert datetime.fromisoformat(stored["updated_at"]) == datetime.fromisoformat(
        second.json()["updated_at"]
    )
    assert datetime.fromisoformat(stored["updated_at"]).utcoffset() is not None
    async with sessions() as session:
        assert (await session.get(AppearanceSettings, 1)) is not None


@pytest.mark.parametrize(
    "body",
    [
        {"logo": "dial", "theme": "dark"},  # Unknown key.
        {"logo": "Dial"},  # Not a preset id.
        {"logo": "../x"},
        {"logo": "a" * 49},
        {"logo": 3},
        {"logo": "data:image/png;base64,AAAA"},  # Uploads are never synced.
        ["dial"],
    ],
)
async def test_invalid_payload_is_rejected(application, body):
    async with client(application, headers=JOB) as http:
        response = await http.put(URL, json=body)
        assert response.status_code == 422
        assert (await http.get(URL)).json()["selection"] == {}


async def test_non_json_is_rejected(application):
    async with client(application, headers=JOB) as http:
        response = await http.put(URL, content=b"logo=dial")
    assert response.status_code == 422


async def test_oversized_body_is_rejected(application):
    async with client(application, headers=JOB) as http:
        response = await http.put(URL, json={"logo": "dial", "pad": "x" * 10_000})
        assert response.status_code == 413

        async def chunks():  # No Content-Length: the limit applies while streaming.
            for chunk in (b'{"logo": "', b"x" * 5000, b'"}'):
                yield chunk

        streamed = await http.put(URL, content=chunks())
        assert streamed.status_code == 413


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-HealthOS-Request": "1", "Origin": "http://attacker.example"}],
)
async def test_put_requires_job_request_header(application, headers):
    async with client(application, headers=headers) as http:
        response = await http.put(URL, json={"logo": "dial"})
        assert response.status_code == 403
        assert (await http.get(URL)).json()["selection"] == {}


async def test_same_origin_browser_put_is_accepted(application):
    headers = {**JOB, "Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"}
    async with client(application, headers=headers) as http:
        assert (await http.put(URL, json={"umay": "umay"})).status_code == 200


async def test_token_is_required_when_configured(monkeypatch, sessions):
    settings = Settings(_env_file=None, app_env="test", app_ui_token=TOKEN, codex_bridge_token=None)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "build_runtime", lambda *_: SimpleNamespace())
    application = main.create_app()

    async def override_session():
        async with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with client(application, headers=JOB) as http:
        assert (await http.get(URL)).status_code == 401
        assert (await http.put(URL, json={"logo": "dial"})).status_code == 401
        authorized = {"Authorization": f"Bearer {TOKEN}"}
        assert (await http.get(URL, headers=authorized)).status_code == 200
        assert (await http.put(URL, json={"logo": "dial"}, headers=authorized)).status_code == 200


def test_openapi_documents_body_and_job_header(application):
    put = application.openapi()["paths"][URL]["put"]
    schema = put["requestBody"]["content"]["application/json"]["schema"]
    assert set(schema["properties"]) == {"logo", "umay", "background"}
    assert schema["additionalProperties"] is False
    assert any(item["name"] == "X-HealthOS-Request" for item in put["parameters"])
