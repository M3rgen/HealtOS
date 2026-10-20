import httpx

from app.main import create_app


async def test_health_endpoint_uses_database(sessions):
    application = create_app()
    application.state.runtime.pipeline_health.sessions = sessions
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/health")
    await application.state.runtime.shutdown()
    assert response.status_code == 200
    assert response.json()["database"] == "healthy"


async def test_liveness_endpoint_does_not_touch_database(monkeypatch):
    application = create_app()

    async def unavailable(**kwargs):
        raise AssertionError("liveness must not inspect the pipeline")

    monkeypatch.setattr(application.state.runtime.pipeline_health, "inspect", unavailable)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/health/live")
    await application.state.runtime.shutdown()

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_database_unavailable_returns_failed_health(monkeypatch):
    application = create_app()

    async def unavailable(**kwargs):
        raise OSError("database unavailable")

    monkeypatch.setattr(application.state.runtime.pipeline_health, "inspect", unavailable)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/health")
    await application.state.runtime.shutdown()

    assert response.status_code == 503
    assert response.json()["status"] == "failed"
    assert response.json()["database"] == "unavailable"


async def test_required_local_api_routes_are_registered():
    application = create_app()
    paths = application.openapi()["paths"]
    registered = {(method.upper(), path) for path, methods in paths.items() for method in methods}
    await application.state.runtime.shutdown()

    assert {
        ("GET", "/health"),
        ("GET", "/health/live"),
        ("GET", "/sync/status"),
        ("GET", "/sync/status/{data_type}"),
        ("POST", "/sync/{data_type}"),
        ("POST", "/sync/all"),
        ("POST", "/backfill/start"),
        ("GET", "/backfill/status"),
        ("POST", "/webhooks/google-health"),
    } <= registered
