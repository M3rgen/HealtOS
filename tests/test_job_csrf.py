from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app import main
from app.core.config import Settings


@pytest.fixture
def application(monkeypatch):
    settings = Settings(_env_file=None, app_env="test")
    jobs = SimpleNamespace(
        specs={"steps": None}, run=AsyncMock(), run_all=AsyncMock(), sequence=7, last_runs={}
    )
    spawned = []

    def spawn(coroutine):
        spawned.append(coroutine)
        coroutine.close()

    runtime = SimpleNamespace(incremental=jobs, backfill=jobs, spawn=spawn, spawned=spawned)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "build_runtime", lambda *_: runtime)
    return main.create_app()


def test_interactive_api_docs_expose_job_request_header(application):
    paths = application.openapi()["paths"]
    for path in ["/sync/all", "/sync/{data_type}", "/backfill/start"]:
        parameters = paths[path]["post"]["parameters"]
        header = next(item for item in parameters if item["name"] == "X-HealthOS-Request")
        assert header["in"] == "header"
        assert header["schema"]["examples"] == ["1"]


@pytest.mark.parametrize("path", ["/sync/all", "/sync/steps", "/backfill/start?data_type=steps"])
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "http://attacker.example", "Content-Type": "application/x-www-form-urlencoded"},
        {"Origin": "null"},
        {"X-HealthOS-Request": "1", "Origin": "http://attacker.example"},
        {"X-HealthOS-Request": "1", "Origin": "https://localhost:8000"},
        {"X-HealthOS-Request": "1", "Origin": "http://localhost:8000/path"},
        {"X-HealthOS-Request": "1", "Origin": "http://user@localhost:8000"},
        {"X-HealthOS-Request": "1", "Origin": "http://localhost:8000?bad=1"},
        {"X-HealthOS-Request": "1", "Sec-Fetch-Site": "cross-site"},
        {"X-HealthOS-Request": "wrong"},
        {"X-HealthOS-Request": "1", "Origin": "http://local\thost:8000"},
        {"X-HealthOS-Request": "1", "Origin": " http://localhost:8000"},
        {"X-HealthOS-Request": "1", "Origin": "http://localhost:8000?"},
        {"X-HealthOS-Request": "1", "Origin": "http://localhost:"},
    ],
)
async def test_job_start_rejects_csrf_before_spawn(application, path, headers):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://localhost:8000"
    ) as client:
        response = await client.post(path, headers=headers)
    assert response.status_code == 403
    assert application.state.runtime.spawned == []


@pytest.mark.parametrize("path", ["/sync/all", "/sync/steps", "/backfill/start?data_type=steps"])
@pytest.mark.parametrize(
    "headers",
    [
        {"X-HealthOS-Request": "1"},
        {"X-HealthOS-Request": "1", "Origin": "http://localhost:8000"},
    ],
)
async def test_explicit_local_client_can_start_jobs(application, path, headers):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://localhost:8000"
    ) as client:
        response = await client.post(path, headers=headers)
    assert response.status_code == 202
    assert len(application.state.runtime.spawned) == 1


@pytest.mark.parametrize("url", ["http://localhost:80", "http://[::1]:8000"])
async def test_default_port_and_ipv6_origins_work(application, url):
    origin = "http://localhost" if url.endswith(":80") else url
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url=url
    ) as client:
        response = await client.post(
            "/sync/all", headers={"Origin": origin, "X-HealthOS-Request": "1"}
        )
    assert response.status_code == 202


async def test_vite_origin_and_duplicate_security_headers(application):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://127.0.0.1:5174"
    ) as client:
        response = await client.post(
            "/sync/all", headers={"Origin": "http://127.0.0.1:5174", "X-HealthOS-Request": "1"}
        )
        assert response.status_code == 202
        for headers in [
            [("X-HealthOS-Request", "1"), ("Origin", "http://127.0.0.1:5174"), ("Origin", "null")],
            [("X-HealthOS-Request", "1"), ("X-HealthOS-Request", "1")],
        ]:
            assert (await client.post("/sync/all", headers=headers)).status_code == 403
        preflight = await client.options(
            "/sync/all",
            headers={
                "Origin": "http://attacker.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-HealthOS-Request",
            },
        )
        assert "access-control-allow-origin" not in preflight.headers
    assert len(application.state.runtime.spawned) == 1


async def test_sync_all_reports_sequence_baseline(application):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://localhost:8000"
    ) as client:
        response = await client.post("/sync/all", headers={"X-HealthOS-Request": "1"})
    assert response.status_code == 202
    assert response.json() == {"status": "accepted", "data_types": ["steps"], "sequence": 7}
