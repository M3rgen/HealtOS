from cryptography.fernet import Fernet
from sqlalchemy import select

from app.core.config import Settings
from app.models.state import OAuthToken
from app.services.credentials import CredentialStore


async def test_rotated_database_token_precedes_stale_environment_fallback(sessions):
    settings = Settings(
        google_refresh_token="environment-fallback",
        google_token_encryption_key=Fernet.generate_key().decode(),
    )
    store = CredentialStore(settings, sessions)

    await store.save_refresh_token("initial", "scope-a scope-b")
    await store.save_refresh_token("rotated")

    assert await store.load_refresh_token() == "rotated"
    async with sessions() as session:
        token = await session.scalar(select(OAuthToken).where(OAuthToken.subject == "me"))
    assert token.scopes == "scope-a scope-b"


async def test_environment_refresh_token_is_used_before_database_onboarding(sessions):
    store = CredentialStore(Settings(google_refresh_token="environment-only"), sessions)

    assert await store.load_refresh_token() == "environment-only"


async def test_verified_connection_creates_encrypted_token_and_identity_together(sessions):
    settings = Settings(_env_file=None, google_token_encryption_key=Fernet.generate_key().decode())
    store = CredentialStore(settings, sessions)
    await store.save_connection("offline", "scope", "account", "legacy")
    async with sessions() as session:
        row = await session.scalar(select(OAuthToken).where(OAuthToken.subject == "me"))
        assert row.encrypted_refresh_token != "offline"
        assert (row.scopes, row.health_user_id, row.legacy_user_id) == (
            "scope",
            "account",
            "legacy",
        )
    assert await store.load_refresh_token() == "offline"
