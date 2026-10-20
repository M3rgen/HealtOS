"""Operational hardening that lives in Compose and deployment files."""

from pathlib import Path

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
