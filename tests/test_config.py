import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_empty_or_placeholder_oauth_credentials_are_not_configured():
    assert not Settings(google_client_id="", google_client_secret="").credentials_configured
    assert not Settings(
        google_client_id="CHANGE_ME", google_client_secret="secret"
    ).credentials_configured
    assert not Settings(
        google_client_id="client", google_client_secret="CHANGE_ME"
    ).credentials_configured


def test_nonempty_oauth_credentials_are_configured():
    assert Settings(google_client_id="client", google_client_secret="secret").credentials_configured


def test_webhook_readiness_requires_public_registration_inputs():
    readiness = Settings(_env_file=None).webhook_readiness
    assert not readiness["external_registration_ready"]
    assert readiness["subscriber_id_valid"]


def test_webhook_readiness_accepts_complete_safe_configuration():
    readiness = Settings(
        google_cloud_project_number="1234567890",
        google_health_webhook_endpoint_uri="https://health.example/webhooks/google-health",
        google_health_webhook_authorization="Bearer local-secret",
    ).webhook_readiness
    assert all(readiness.values())


def test_production_rejects_missing_and_placeholder_secrets():
    with pytest.raises(ValidationError, match="unsafe or missing production configuration"):
        Settings(app_env="production", _env_file=None)


def test_production_accepts_explicit_non_placeholder_secrets():
    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://health:random-password@postgres:5432/health",
        google_client_id="client-id",
        google_client_secret="client-secret",
        google_token_encryption_key="encryption-key",
        google_health_webhook_endpoint_uri="https://health.example/webhooks/google-health",
        google_health_webhook_authorization="Bearer webhook-secret",
        app_ui_token="u" * 32,
        _env_file=None,
    )
    assert settings.app_env == "production"
