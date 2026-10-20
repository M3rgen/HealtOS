"""The Compose deployment must run the production guard and the guard must bite."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.core.config import Settings
from scripts.verify_configuration import check_compose_runtime, parse_env, service_environment

ROOT = Path(__file__).resolve().parents[1]


def load_compose(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def test_compose_app_runs_in_production_and_dev_override_relaxes_it():
    base = service_environment(load_compose("docker-compose.yml")["services"]["app"])
    dev = service_environment(load_compose("docker-compose.dev.yml")["services"]["app"])
    assert base["APP_ENV"] == "production"
    assert dev["APP_ENV"] == "development"


def test_verifier_rejects_compose_without_production_app_env():
    errors = check_compose_runtime(
        {"services": {"app": {"environment": {"APP_ENV": "development"}}}},
        {"services": {"app": {"environment": ["APP_ENV=development"]}}},
    )
    assert "docker-compose.yml app service must set APP_ENV: production" in errors
    assert not any("docker-compose.dev.yml" in error for error in errors)


def test_compose_production_with_template_placeholders_refuses_to_start(monkeypatch):
    """Simulate the app container: .env.example as env_file, compose environment on top."""
    for name in list(Settings.model_fields):
        monkeypatch.delenv(name.upper(), raising=False)
    for name, value in parse_env(ROOT / ".env.example").items():
        monkeypatch.setenv(name, value)
    compose_env = service_environment(load_compose("docker-compose.yml")["services"]["app"])
    monkeypatch.setenv("APP_ENV", compose_env["APP_ENV"])
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://health:strong-generated@postgres:5432/health"
    )
    with pytest.raises(ValidationError, match="unsafe or missing production configuration"):
        Settings(_env_file=None)


def test_production_rejects_change_me_placeholders():
    with pytest.raises(ValidationError, match="GOOGLE_CLIENT_ID"):
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://health:random-password@postgres:5432/health",
            google_client_id="CHANGE_ME",
            google_client_secret="client-secret",
            google_token_encryption_key="encryption-key",
            google_health_webhook_endpoint_uri="https://webhook.example.com/webhooks/google-health",
            google_health_webhook_authorization="Bearer webhook-secret",
            _env_file=None,
        )
