from types import SimpleNamespace

import httpx
import pytest

from app import main
from app.core.config import Settings
from app.core.security_headers import app_policy


def policy(header: str) -> dict[str, list[str]]:
    directives = {}
    for item in header.split(";"):
        name, *sources = item.split()
        assert name not in directives, f"duplicate directive {name}"
        directives[name] = sources
    return directives


@pytest.fixture
def application(monkeypatch):
    settings = Settings(
        _env_file=None,
        app_env="test",
        google_health_webhook_endpoint_uri="https://hooks.example/webhooks/google-health",
        google_health_webhook_authorization="Bearer webhook-secret",
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "build_runtime", lambda *_: SimpleNamespace())
    return main.create_app()


def client(application, base="http://localhost:8000"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url=base)


def assert_hardened(response):
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"
    csp = policy(response.headers["content-security-policy"])
    assert csp["frame-ancestors"] == ["'none'"]
    return csp


async def test_dashboard_policy(application):
    async with client(application) as http:
        response = await http.get("/health/live")
    csp = assert_hardened(response)
    assert csp["default-src"] == ["'self'"]
    assert csp["script-src"] == ["'self'", "'wasm-unsafe-eval'"]
    assert csp["style-src"] == ["'self'"]
    assert csp["img-src"] == ["'self'", "blob:", "data:"]
    assert csp["font-src"] == ["'self'", "data:"]
    assert csp["worker-src"] == ["'self'", "blob:"]
    assert csp["connect-src"] == ["'self'", "blob:", "ws://localhost:8000"]
    assert csp["object-src"] == ["'none'"] and csp["base-uri"] == ["'none'"]
    joined = response.headers["content-security-policy"]
    assert "'unsafe-eval'" not in joined
    assert "'unsafe-inline'" not in " ".join(csp["script-src"] + csp["style-src"])
    assert "http:" not in joined and "https:" not in joined


@pytest.mark.parametrize(
    "host, expected",
    [
        ("localhost:18080", "ws://localhost:18080"),  # SSH tunnel on another local port
        ("127.0.0.1:8000", "ws://127.0.0.1:8000"),
        ("LOCALHOST", "ws://localhost"),
    ],
)
async def test_websocket_source_follows_the_loopback_host(application, host, expected):
    async with client(application) as http:
        response = await http.get("/health/live", headers={"Host": host})
    connect = policy(response.headers["content-security-policy"])["connect-src"]
    assert connect == ["'self'", "blob:", expected]


@pytest.mark.parametrize(
    "host", [None, "[::1]:8000", "hooks.example", "attacker.example", "x;script-src *"]
)
def test_untrusted_or_ipv6_hosts_add_no_websocket_source(host):
    assert policy(app_policy(host))["connect-src"] == ["'self'", "blob:"]


async def test_headers_on_errors_and_auth_responses(application, monkeypatch):
    async with client(application) as http:
        missing = await http.get("/api/dashboard/does-not-exist")
        bad_host = await http.get("/health/live", headers={"Host": "attacker.example"})
    assert missing.status_code == 404 and bad_host.status_code == 400
    assert_hardened(missing)
    assert_hardened(bad_host)
    secured = build_with_token(monkeypatch)
    async with client(secured) as http:
        denied = await http.get("/api/dashboard")
        redirect = await http.get("/", headers={"Accept": "text/html"})
    assert denied.status_code == 401 and redirect.status_code == 303
    assert_hardened(denied)
    assert_hardened(redirect)


def build_with_token(monkeypatch):
    settings = Settings(_env_file=None, app_env="test", app_ui_token="k" * 40)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "build_runtime", lambda *_: SimpleNamespace())
    return main.create_app()


async def test_login_page_keeps_its_own_stricter_policy(monkeypatch):
    async with client(build_with_token(monkeypatch)) as http:
        response = await http.get("/login")
    csp = assert_hardened(response)
    assert csp["default-src"] == ["'none'"]
    assert csp["style-src"][0].startswith("'sha256-")
    assert csp["form-action"] == ["'self'"]


async def test_webhook_receiver_gets_headers_too(application):
    async with client(application, "https://hooks.example") as http:
        response = await http.post(
            "/webhooks/google-health",
            json={"type": "verification"},
            headers={"Authorization": "Bearer webhook-secret"},
        )
    assert response.status_code == 201
    assert assert_hardened(response)["connect-src"] == ["'self'", "blob:"]


async def test_interactive_docs_get_their_cdn_exception_only(application):
    async with client(application) as http:
        docs = await http.get("/docs")
        dashboard_csp = (await http.get("/health/live")).headers["content-security-policy"]
    csp = assert_hardened(docs)
    assert "https://cdn.jsdelivr.net" in csp["script-src"]
    assert "cdn.jsdelivr.net" not in dashboard_csp
