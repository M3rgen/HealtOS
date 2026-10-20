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
