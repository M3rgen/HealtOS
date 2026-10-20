"""Operational hardening that lives in Compose and deployment files."""

from pathlib import Path

import pytest
import yaml

from scripts.verify_configuration import check_compose_runtime

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
DEV = yaml.safe_load((ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8"))


def test_app_and_postgres_logs_rotate():
    for name in ("app", "postgres"):
        logging = COMPOSE["services"][name]["logging"]
        assert logging["driver"] == "json-file"
        assert logging["options"] == {"max-size": "20m", "max-file": "5"}


def test_verifier_reports_missing_log_rotation():
    broken = yaml.safe_load(yaml.safe_dump(COMPOSE))
    del broken["services"]["postgres"]["logging"]
    errors = check_compose_runtime(broken, DEV)
    assert "postgres service lacks json-file log rotation (max-size, max-file)" in errors


def test_codex_bridge_never_binds_all_interfaces(monkeypatch, tmp_path):
    from scripts import start_codex_bridge as bridge

    monkeypatch.setattr(bridge, "ROOT", tmp_path)
    monkeypatch.setattr(bridge, "docker0_address", lambda: None)
    monkeypatch.delenv("CODEX_BRIDGE_BIND_HOST", raising=False)
    assert bridge.bind_host() == "127.0.0.1"
    monkeypatch.setattr(bridge, "docker0_address", lambda: "172.17.0.1")
    assert bridge.bind_host() == "172.17.0.1"
    (tmp_path / ".env").write_text("CODEX_BRIDGE_BIND_HOST=127.0.0.1\n", encoding="utf-8")
    assert bridge.bind_host() == "127.0.0.1"
    for unsafe in ("0.0.0.0", "::", "[::]", "localhost-ish"):
        monkeypatch.setenv("CODEX_BRIDGE_BIND_HOST", unsafe)
        with pytest.raises(SystemExit):
            bridge.bind_host()
    monkeypatch.setenv("CODEX_BRIDGE_BIND_HOST", "::1")
    assert bridge.bind_host() == "[::1]"


def test_app_resolves_host_gateway_and_repo_has_no_wildcard_bridge():
    from scripts.verify_configuration import check_codex_bridge_binding

    assert "host.docker.internal:host-gateway" in COMPOSE["services"]["app"]["extra_hosts"]
    assert check_codex_bridge_binding() == []


def test_postgres_and_backup_drop_capabilities():
    postgres = COMPOSE["services"]["postgres"]
    assert postgres["cap_drop"] == ["ALL"]
    assert set(postgres["cap_add"]) <= {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"}
    assert "no-new-privileges:true" in postgres["security_opt"]
    backup = COMPOSE["services"]["backup"]
    assert backup["cap_drop"] == ["ALL"] and backup["read_only"] is True
    assert "volumes" not in backup


def test_alembic_ini_holds_no_database_url():
    import configparser

    parser = configparser.ConfigParser()
    parser.read(ROOT / "alembic.ini", encoding="utf-8")
    assert parser["alembic"]["sqlalchemy.url"] == ""


def test_ui_smoke_script_compiles_and_authenticates():
    from scripts.verify_compose import UI_SMOKE_SCRIPT

    compile(UI_SMOKE_SCRIPT, "ui_smoke", "exec")
    assert "Bearer " in UI_SMOKE_SCRIPT and "'X-HealthOS-Request': '1'" in UI_SMOKE_SCRIPT
    assert COMPOSE["services"]["app"]["environment"]["APP_UI_TOKEN"].startswith("${APP_UI_TOKEN:?")


def test_ui_smoke_title_check_matches_the_served_page():
    # The repo is HealtOS, the product HealthOS; the smoke test must look for the latter.
    import re

    from scripts.verify_compose import UI_SMOKE_SCRIPT

    expected = re.search(r"assert '(<title>[^']+)' in html", UI_SMOKE_SCRIPT)
    page = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert expected and expected.group(1) in page


def _run_smoke_scripts_against_fake_app(monkeypatch, token: str):
    """Execute the in-container smoke scripts with urllib answered like UITokenMiddleware."""
    import io
    import json
    import urllib.error
    import urllib.request
    from urllib.parse import urlsplit

    from app.core.auth import EXEMPT
    from scripts.verify_compose import HEALTH_SMOKE_SCRIPT, UI_SMOKE_SCRIPT

    saved: list[dict] = []
    calls: list[tuple[str, str, bool]] = []

    class Response(io.BytesIO):
        def __init__(self, body: bytes, status: int = 200) -> None:
            super().__init__(body)
            self.status = status

    def payload(method: str, path: str, data: bytes | None) -> Response:
        if path == "/":
            return Response(
                b'<title>HealthOS</title><script src="/assets/a.js"></script>'
                b'<link href="/assets/a.css">'
            )
        if path.startswith("/assets/") or path == "/health/live":
            return Response(b"{}")
        if path == "/health":
            body = {
                "database": "healthy",
                "service": "personal-health-mirror",
                "scheduler": "disabled",
            }
            return Response(json.dumps(body).encode())
        if path == "/api/dashboard":
            profile = {"muscle": {"value": saved[-1]["value"]}} if saved else {}
            days = [{"steps": None}] * 7
            return Response(json.dumps({"days": days, "profile": profile}).encode())
        if path == "/api/dashboard/measurements" and method == "POST":
            saved.append(json.loads(data or b"{}"))
            return Response(b"{}", status=201)
        if path == "/api/dashboard/measurements":
            return Response(json.dumps([{**m, "source": "Manuel"} for m in saved]).encode())
        if path == "/api/dashboard/export.csv":
            return Response(b"date,sleep_seconds,timezone\n2026-09-29,,UTC\n")
        raise AssertionError(f"unexpected smoke request {method} {path}")

    def urlopen(request, *args, **kwargs):
        if isinstance(request, str):
            request = urllib.request.Request(request)
        method, path = request.get_method(), urlsplit(request.full_url).path
        authorized = request.get_header("Authorization") == f"Bearer {token}"
        calls.append((method, path, authorized))
        if not authorized and (method, path) not in EXEMPT:
            raise urllib.error.HTTPError(request.full_url, 401, "auth_required", None, None)
        return payload(method, path, request.data)

    monkeypatch.setenv("APP_UI_TOKEN", token)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    for script in (HEALTH_SMOKE_SCRIPT, UI_SMOKE_SCRIPT):
        exec(compile(script, "smoke", "exec"), {})
    return calls


def test_smoke_scripts_send_the_ui_token_to_every_protected_route(monkeypatch):
    from app.core.auth import EXEMPT

    calls = _run_smoke_scripts_against_fake_app(monkeypatch, "t" * 40)
    anonymous = {
        (method, path) for method, path, ok in calls if not ok and (method, path) not in EXEMPT
    }
    # Only the explicit "rejected without a token" probes go out unauthenticated.
    assert anonymous == {("GET", "/health"), ("GET", "/api/dashboard")}
    assert ("GET", "/health", True) in calls and ("GET", "/health/live", False) in calls
    assert ("POST", "/api/dashboard/measurements", True) in calls


def test_smoke_scripts_fail_when_the_bearer_header_is_missing(monkeypatch):
    import urllib.error
    import urllib.request

    real = urllib.request.Request

    class Unauthenticated(real):  # strips the bearer header, as a forgotten header would
        def __init__(self, *args, headers=None, **kwargs):
            headers = {k: v for k, v in (headers or {}).items() if k != "Authorization"}
            super().__init__(*args, headers=headers, **kwargs)

    monkeypatch.setattr(urllib.request, "Request", Unauthenticated)
    with pytest.raises(urllib.error.HTTPError):
        _run_smoke_scripts_against_fake_app(monkeypatch, "t" * 40)
