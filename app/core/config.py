from __future__ import annotations

import re
from datetime import date, timedelta
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Google's webhook signing keyset is only ever fetched from Google's static host;
# an attacker-controlled keyset URL would let forged webhooks pass verification.
GOOGLE_KEYSET_URL_PREFIX = "https://www.gstatic.com/"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://health:health@postgres:5432/health"
    codex_bridge_url: str = "ws://host.docker.internal:8766"
    codex_bridge_token: str | None = None
    # Single-user UI/API bearer token; empty disables the barrier outside production.
    app_ui_token: str | None = None

    google_health_base_url: str = "https://health.googleapis.com/v4"
    google_client_id: str = "CHANGE_ME"
    google_client_secret: str = "CHANGE_ME"
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    google_refresh_token: str | None = None
    google_token_encryption_key: str | None = None
    google_cloud_project_number: str | None = None
    google_health_webhook_endpoint_uri: str | None = None
    google_health_webhook_subscriber_id: str = "personal-health-mirror"
    google_health_webhook_authorization: str = "CHANGE_ME"
    google_health_webhook_max_body_bytes: int = Field(
        default=2 * 1024 * 1024, ge=1024, le=16 * 1024 * 1024
    )
    google_health_webhook_public_key_url: str = (
        "https://www.gstatic.com/googlehealthapi/webhooks/webhooks_public_keyset.json"
    )

    backfill_start_date: date = date(2010, 1, 1)
    backfill_window_days: int = Field(default=31, ge=1, le=366)
    google_api_page_size: int = Field(default=10_000, ge=1, le=10_000)
    google_api_max_retries: int = Field(default=6, ge=0, le=12)
    google_api_min_request_interval_seconds: float = Field(default=0.25, ge=0, le=10)
    sync_concurrency: int = Field(default=2, ge=1, le=10)
    extra_data_types: Annotated[list[str], NoDecode] = Field(default_factory=list)
    enabled_data_types: Annotated[list[str], NoDecode] = Field(default_factory=list)

    reconciliation_enabled: bool = True
    reconciliation_daily_hour_utc: int = Field(default=2, ge=0, le=23)
    reconciliation_weekly_day: str = "sun"
    reconciliation_weekly_hour_utc: int = Field(default=3, ge=0, le=23)
    reconciliation_daily_days: int = Field(default=7, ge=1)
    reconciliation_weekly_days: int = Field(default=90, ge=1)

    watchdog_enabled: bool = True
    watchdog_interval_seconds: int = Field(default=900, ge=60, le=86_400)
    pipeline_stale_threshold_minutes: int = Field(default=120, ge=15, le=10_080)
    # raw_health_data freshness summary (data_freshness, latest_health_write) is
    # recomputed at most this often; /health serves the cached result in between.
    pipeline_freshness_refresh_seconds: int = Field(default=3_600, ge=60, le=86_400)
    recovery_cooldown_minutes: int = Field(default=360, ge=15, le=43_200)
    startup_gap_threshold_minutes: int = Field(default=120, ge=15, le=10_080)
    automatic_recovery_enabled: bool = True
    automatic_recovery_max_days: int = Field(default=7, ge=1, le=30)

    @model_validator(mode="after")
    def reject_unsafe_production_configuration(self) -> Settings:
        if self.app_env.casefold() not in {"production", "prod"}:
            return self

        unsafe: list[str] = []
        required = {
            "DATABASE_URL": self.database_url,
            "GOOGLE_CLIENT_ID": self.google_client_id,
            "GOOGLE_CLIENT_SECRET": self.google_client_secret,
            "GOOGLE_TOKEN_ENCRYPTION_KEY": self.google_token_encryption_key,
            "GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION": self.google_health_webhook_authorization,
            "APP_UI_TOKEN": self.app_ui_token,
        }
        for name, value in required.items():
            normalized = (value or "").strip()
            if not normalized or normalized.upper() in {"CHANGE_ME", "CHANGEME", "TODO"}:
                unsafe.append(name)
        if self.database_url == "postgresql+asyncpg://health:health@postgres:5432/health":
            unsafe.append("DATABASE_URL")
        if not (
            self.google_health_webhook_endpoint_uri
            and self.google_health_webhook_endpoint_uri.startswith("https://")
        ):
            unsafe.append("GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI")
        if unsafe:
            names = ", ".join(sorted(set(unsafe)))
            raise ValueError(f"unsafe or missing production configuration: {names}")
        return self

    @field_validator("app_ui_token", mode="before")
    @classmethod
    def valid_ui_token(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if not isinstance(value, str) or not re.fullmatch(r"[\x21-\x7e]{32,512}", value):
            raise ValueError(
                "APP_UI_TOKEN must be 32-512 printable ASCII characters without spaces; "
                'generate one with: python -c "import secrets;print(secrets.token_urlsafe(32))"'
            )
        return value

    @field_validator("extra_data_types", "enabled_data_types", mode="before")
    @classmethod
    def parse_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("google_health_webhook_public_key_url")
    @classmethod
    def require_google_keyset_host(cls, value: str) -> str:
        if not value.startswith(GOOGLE_KEYSET_URL_PREFIX):
            raise ValueError(
                f"webhook public keyset URL must start with {GOOGLE_KEYSET_URL_PREFIX}"
            )
        return value

    @property
    def credentials_configured(self) -> bool:
        return all(
            value and value != "CHANGE_ME"
            for value in (self.google_client_id, self.google_client_secret)
        )

    @property
    def webhook_readiness(self) -> dict[str, bool]:
        authorization = self.google_health_webhook_authorization.strip()
        result = {
            "project_number_configured": bool(
                self.google_cloud_project_number and self.google_cloud_project_number.isdigit()
            ),
            "https_endpoint_configured": bool(
                self.google_health_webhook_endpoint_uri
                and self.google_health_webhook_endpoint_uri.startswith("https://")
            ),
            "authorization_configured": bool(
                authorization != "CHANGE_ME" and authorization.startswith(("Bearer ", "Basic "))
            ),
            "subscriber_id_valid": bool(
                re.fullmatch(
                    r"[a-z]([a-z0-9-]{2,34}[a-z0-9])",
                    self.google_health_webhook_subscriber_id,
                )
            ),
        }
        result["external_registration_ready"] = all(result.values())
        return result

    def safety_window(self, data_type: str) -> timedelta:
        if data_type == "heart-rate":
            return timedelta(minutes=60)
        if data_type in {"sleep", "daily-sleep-temperature-derivations"}:
            return timedelta(hours=72)
        if data_type.startswith("daily-"):
            return timedelta(days=7)
        if data_type in {"steps", "distance", "active-energy-burned", "active-zone-minutes"}:
            return timedelta(hours=12)
        return timedelta(hours=24)


@lru_cache
def get_settings() -> Settings:
    return Settings()
