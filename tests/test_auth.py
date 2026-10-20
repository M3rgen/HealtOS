import base64
import hashlib
import hmac
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from app import main
from app.core import auth
from app.core.config import Settings

TOKEN = "t" * 20 + "-test-ui-token-0123456789"
BASE = "http://localhost:8000"


def build(monkeypatch, token=TOKEN, **overrides):
    settings = Settings(
        _env_file=None,
        app_env="test",
        app_ui_token=token,
        codex_bridge_token=None,
        google_health_webhook_endpoint_uri="https://hooks.example/webhooks/google-health",
        google_health_webhook_authorization="Bearer webhook-secret",
        **overrides,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "build_runtime", lambda *_: SimpleNamespace())
    application = main.create_app()

    @application.get("/api/probe")
    async def api_probe():
        return {"reached": True}

    @application.get("/page-probe")
    async def page_probe():
        return {"reached": True}

    return application


@pytest.fixture
def application(monkeypatch):
    return build(monkeypatch)


# What the old login set: HMAC-SHA256(token, "healthos-ui-cookie-v1"), valid forever.
LEGACY_COOKIE = (
    base64.urlsafe_b64encode(
        hmac.new(TOKEN.encode(), b"healthos-ui-cookie-v1", hashlib.sha256).digest()
    )
    .decode()
    .rstrip("=")
)


async def login_session(application, base=BASE) -> str:
    async with client(application, base) as http:
        response = await http.post("/login", data={"token": TOKEN, "next": "/"})
    assert response.status_code == 303
    return response.cookies[auth.COOKIE_NAME]


def client(application, base=BASE):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url=base)


async def test_api_without_token_is_401_json(application):
    async with client(application) as http:
        response = await http.get("/api/probe")
    assert response.status_code == 401
    assert response.json() == {"detail": "auth_required"}
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/sync/status", "/backfill/status", "/health", "/docs"])
async def test_other_private_routes_need_token(application, path):
    async with client(application) as http:
        response = await http.get(path)
    assert response.status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": f"Bearer {TOKEN}"},
        {"Authorization": f"bearer {TOKEN}"},
        {"Cookie": f"{auth.COOKIE_NAME}={{session}}"},
        {"Cookie": f"other=1; {auth.COOKIE_NAME}={{session}}"},
    ],
)
async def test_right_token_via_header_or_cookie(application, headers):
    session = await login_session(application)
    headers = {name: value.format(session=session) for name, value in headers.items()}
    async with client(application) as http:
        response = await http.get("/api/probe", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"reached": True}


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer wrong-token-wrong-token-wrong-token"},
        {"Authorization": f"Bearer {TOKEN[:-1]}"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": TOKEN},
        {"Cookie": f"{auth.COOKIE_NAME}={TOKEN}"},
        {"Cookie": f"{auth.COOKIE_NAME}=wrong"},
        {"Cookie": f"x{auth.COOKIE_NAME}={{session}}"},
        # The pre-session cookie, a fixed HMAC of the token: never valid again.
        {"Cookie": f"{auth.COOKIE_NAME}={LEGACY_COOKIE}"},
    ],
)
async def test_wrong_token_is_rejected(application, headers):
    session = await login_session(application)
    headers = {name: value.format(session=session) for name, value in headers.items()}
    async with client(application) as http:
        response = await http.get("/api/probe", headers=headers)
    assert response.status_code == 401


async def test_duplicate_authorization_headers_are_rejected(application):
    async with client(application) as http:
        response = await http.get(
            "/api/probe",
            headers=[("Authorization", f"Bearer {TOKEN}"), ("Authorization", "Bearer x")],
        )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "headers",
    [{"Accept": "text/html,application/xhtml+xml"}, {"Sec-Fetch-Mode": "navigate"}],
)
async def test_html_navigation_redirects_to_login(application, headers):
    async with client(application) as http:
        response = await http.get("/page-probe?x=1&y=2", headers=headers)
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == "/login"
    assert parse_qs(location.query) == {"next": ["/page-probe?x=1&y=2"]}


async def test_api_navigation_still_gets_401(application):
    async with client(application) as http:
        response = await http.get("/api/probe", headers={"Accept": "text/html"})
    assert response.status_code == 401


async def test_unauthenticated_post_is_401_not_redirect(application):
    async with client(application) as http:
        response = await http.post("/sync/all", headers={"Accept": "text/html"})
    assert response.status_code == 401


async def test_liveness_and_webhook_work_without_token(application):
    async with client(application) as http:
        live = await http.get("/health/live")
        hook = await http.post(
            "/webhooks/google-health",
            json={"type": "verification"},
            headers={"Authorization": "Bearer webhook-secret"},
        )
    assert live.status_code == 200
    assert hook.status_code == 201


async def test_public_webhook_host_still_reaches_receiver(application):
    async with client(application, "https://hooks.example") as http:
        hook = await http.post(
            "/webhooks/google-health",
            json={"type": "verification"},
            headers={"Authorization": "Bearer webhook-secret"},
        )
        other = await http.get("/health/live")
    assert hook.status_code == 201
    assert other.status_code == 400  # host validation still runs first


async def test_oauth_callback_is_left_to_signed_state(application):
    async with client(application) as http:
        response = await http.get("/auth/google/callback?code=x&state=forged")
    assert response.status_code == 400  # rejected by the HMAC state check, not the login


async def test_login_page_renders_without_token(application):
    async with client(application) as http:
        response = await http.get("/login?next=/api/probe")
    assert response.status_code == 200
    assert "Erişim anahtarı" in response.text and "Giriş yap" in response.text
    assert 'value="/api/probe"' in response.text
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "'sha256-" in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["cache-control"] == "no-store"
    assert "http://" not in response.text and "https://" not in response.text


async def test_login_sets_strict_httponly_cookie_and_redirects(application):
    async with client(application) as http:
        response = await http.post(
            "/login",
            data={"token": TOKEN, "next": "/page-probe?a=1"},
            headers={"Origin": BASE},
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/page-probe?a=1"
        cookie = response.headers["set-cookie"]
        attributes = {part.strip().lower() for part in cookie.split(";")}
        assert cookie.startswith(f"{auth.COOKIE_NAME}=")
        assert f"max-age={auth.COOKIE_MAX_AGE}" in attributes
        assert {"httponly", "samesite=strict", "path=/"} <= attributes
        assert "secure" not in attributes
        assert TOKEN not in cookie
        follow = await http.get("/api/probe")  # the client keeps the cookie
    assert follow.status_code == 200


async def test_login_rejects_wrong_token(application):
    async with client(application) as http:
        response = await http.post("/login", data={"token": "nope", "next": "/"})
    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert "yanlış" in response.text


@pytest.mark.parametrize(
    "target",
    [
        "https://attacker.example/",
        "//attacker.example/",
        "/\\attacker.example",
        "\\\\attacker.example",
        "/\t/attacker.example",
        "javascript:alert(1)",
        "http:/attacker.example",
        "attacker.example",
        "/login?next=//attacker.example",
        '/"><script>x</script>',
        "",
    ],
)
async def test_login_never_redirects_off_site(application, target):
    async with client(application) as http:
        response = await http.post("/login", data={"token": TOKEN, "next": target})
        page = await http.get("/login", params={"next": target})
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert page.status_code == 303 and page.headers["location"] == "/"  # cookie now set


@pytest.mark.parametrize("target", ["https://attacker.example/", "//attacker.example"])
async def test_login_page_sanitizes_next(application, target):
    async with client(application) as http:
        response = await http.get("/login", params={"next": target})
    assert 'name="next" value="/"' in response.text


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://attacker.example"},
        {"Origin": "http://localhost:3000"},
        {"Origin": "https://localhost:8000"},
        {"Origin": "null"},
        {"Origin": "http://localhost:8000/path"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
async def test_login_rejects_cross_origin_posts(application, headers):
    async with client(application) as http:
        response = await http.post("/login", data={"token": TOKEN, "next": "/"}, headers=headers)
    assert response.status_code == 403
    assert "set-cookie" not in response.headers


async def test_login_accepts_same_origin_on_tunnel_port(application):
    async with client(application, "http://localhost:18080") as http:
        response = await http.post(
            "/login",
            data={"token": TOKEN, "next": "/"},
            headers={"Origin": "http://localhost:18080", "Sec-Fetch-Site": "same-origin"},
        )
    assert response.status_code == 303


async def test_login_rejects_oversized_form(application):
    async with client(application) as http:
        response = await http.post("/login", content=b"token=" + b"a" * 10_000)
    assert response.status_code == 413


def test_websocket_requires_token(application):
    test_client = TestClient(application, base_url=BASE)  # no lifespan: no database
    with pytest.raises(WebSocketDisconnect) as closed:
        with test_client.websocket_connect(
            "ws://localhost:8000/api/codex/chat", headers={"Origin": BASE}
        ):
            pass
    assert (closed.value.code, closed.value.reason) == (1008, "auth_required")
    session = application.state.ui_sessions.issue()
    cookie = {"Cookie": f"{auth.COOKIE_NAME}={session}"}
    with test_client.websocket_connect(
        "ws://localhost:8000/api/codex/chat", headers={"Origin": BASE, **cookie}
    ) as socket:
        assert socket.receive_json()["type"] == "error"  # bridge not configured
    # A revoked session no longer opens the chat socket.
    application.state.ui_sessions.revoke(session)
    with pytest.raises(WebSocketDisconnect) as closed:
        with test_client.websocket_connect(
            "ws://localhost:8000/api/codex/chat", headers={"Origin": BASE, **cookie}
        ):
            pass
    assert closed.value.code == 1008


def test_chat_route_checks_the_cookie_itself():
    from fastapi import FastAPI

    from app.api import codex

    bare = FastAPI()  # no middleware: the route must still refuse on its own
    bare.state.settings = Settings(_env_file=None, app_ui_token=TOKEN, codex_bridge_token=None)
    bare.state.ui_sessions = auth.SessionStore()
    bare.include_router(codex.router)
    test_client = TestClient(bare)
    url = "ws://localhost:8000/api/codex/chat"
    with pytest.raises(WebSocketDisconnect) as closed:
        with test_client.websocket_connect(url, headers={"Origin": BASE}):
            pass
    assert closed.value.code == 1008
    cookie = f"{auth.COOKIE_NAME}={bare.state.ui_sessions.issue()}"
    with test_client.websocket_connect(url, headers={"Origin": BASE, "Cookie": cookie}) as socket:
        assert socket.receive_json()["type"] == "error"


async def test_empty_token_disables_auth_outside_production(monkeypatch):
    application = build(monkeypatch, token="")
    assert application.state.settings.app_ui_token is None
    async with client(application) as http:
        assert (await http.get("/api/probe")).status_code == 200
        assert (await http.get("/login?next=/sync")).headers["location"] == "/sync"


def production(**overrides):
    values = {
        "app_env": "production",
        "database_url": "postgresql+asyncpg://health:random-password@postgres:5432/health",
        "google_client_id": "client-id",
        "google_client_secret": "client-secret",
        "google_token_encryption_key": "encryption-key",
        "google_health_webhook_endpoint_uri": "https://health.example/webhooks/google-health",
        "google_health_webhook_authorization": "Bearer webhook-secret",
        "_env_file": None,
    }
    return Settings(**{**values, **overrides})


@pytest.mark.parametrize("token", [None, "", "   "])
def test_production_refuses_to_start_without_ui_token(token):
    with pytest.raises(ValidationError, match="APP_UI_TOKEN"):
        production(app_ui_token=token)


def test_production_accepts_strong_ui_token():
    assert production(app_ui_token=TOKEN).app_ui_token == TOKEN


@pytest.mark.parametrize("token", ["short", "x" * 31, "has space" * 5, "ü" * 40])
def test_weak_tokens_are_rejected_in_every_environment(token):
    with pytest.raises(ValidationError, match="APP_UI_TOKEN"):
        Settings(_env_file=None, app_env="development", app_ui_token=token)


def test_env_example_documents_token_generation():
    from pathlib import Path

    template = (Path(__file__).parents[1] / ".env.example").read_text()
    assert "\nAPP_UI_TOKEN=\n" in template
    assert "secrets.token_urlsafe(32)" in template


async def test_each_login_gets_its_own_random_session(application):
    first = await login_session(application)
    second = await login_session(application)
    assert first != second and len(first) >= 43
    assert TOKEN not in first and first != LEGACY_COOKIE


async def test_sessions_expire_after_the_cookie_lifetime(application):
    store = application.state.ui_sessions
    now = [1_000.0]
    store.clock = lambda: now[0]
    session = await login_session(application)
    cookie = {"Cookie": f"{auth.COOKIE_NAME}={session}"}
    async with client(application) as http:
        now[0] += auth.COOKIE_MAX_AGE - 1
        assert (await http.get("/api/probe", headers=cookie)).status_code == 200
        now[0] += 2
        assert (await http.get("/api/probe", headers=cookie)).status_code == 401


async def test_logout_revokes_the_session_and_clears_the_cookie(application):
    session = await login_session(application)
    cookie = {"Cookie": f"{auth.COOKIE_NAME}={session}"}
    async with client(application) as http:
        response = await http.post("/logout", headers={**cookie, "Origin": BASE})
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        cleared = response.headers["set-cookie"].lower()
        assert cleared.startswith(f"{auth.COOKIE_NAME}=") and "max-age=0" in cleared
        # Even a copy of the cookie kept elsewhere is dead now.
        assert (await http.get("/api/probe", headers=cookie)).status_code == 401


@pytest.mark.parametrize(
    "headers", [{"Origin": "http://localhost:3000"}, {"Sec-Fetch-Site": "same-site"}]
)
async def test_logout_rejects_cross_origin_posts(application, headers):
    session = await login_session(application)
    cookie = {"Cookie": f"{auth.COOKIE_NAME}={session}"}
    async with client(application) as http:
        response = await http.post("/logout", headers={**cookie, **headers})
        assert response.status_code == 403
        assert (await http.get("/api/probe", headers=cookie)).status_code == 200


async def test_logout_without_a_session_still_clears_the_cookie(application):
    async with client(application) as http:
        response = await http.post("/logout", headers={"Origin": BASE})
    assert response.status_code == 303
    assert "max-age=0" in response.headers["set-cookie"].lower()


async def test_login_works_on_the_healthos_localhost_name(application):
    base = "http://healthos.localhost:8000"
    session = await login_session(application, base)
    async with client(application, base) as http:
        response = await http.get("/api/probe", headers={"Cookie": f"healthos_ui={session}"})
    assert response.status_code == 200


def test_session_store_is_bounded():
    store = auth.SessionStore()
    sessions = [store.issue() for _ in range(auth.SessionStore.MAX_SESSIONS + 5)]
    assert not store.valid(sessions[0])
    assert store.valid(sessions[-1])


@pytest.mark.parametrize("path", ["/login", "/health/live"])
async def test_exempt_pages_answer_head(application, path):
    async with client(application) as http:
        response = await http.head(path)
    assert response.status_code == 200


async def test_login_with_trailing_slash_redirects_to_the_login_page(application):
    async with client(application) as http:
        response = await http.get("/login/?next=/sync")
    assert response.status_code in {307, 308}
    assert response.headers["location"].endswith("/login?next=/sync")


async def test_failed_login_is_slowed_down(application, monkeypatch):
    from app.api import login

    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(login.asyncio, "sleep", fake_sleep)
    async with client(application) as http:
        await http.post("/login", data={"token": "nope", "next": "/"})
        await http.post("/login", data={"token": TOKEN, "next": "/"})
    assert delays == [login.FAILED_LOGIN_DELAY_SECONDS]
