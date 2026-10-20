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
