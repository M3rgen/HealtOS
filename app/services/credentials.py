from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.models.state import OAuthToken


class CredentialStoreError(RuntimeError):
    pass


class CredentialStore:
    def __init__(self, settings: Settings, sessions: async_sessionmaker):
        self.settings = settings
        self.sessions = sessions

    def _fernet(self) -> Fernet:
        if not self.settings.google_token_encryption_key:
            raise CredentialStoreError("GOOGLE_TOKEN_ENCRYPTION_KEY is not configured")
        try:
            return Fernet(self.settings.google_token_encryption_key.encode())
        except (ValueError, TypeError) as exc:
            raise CredentialStoreError(
                "GOOGLE_TOKEN_ENCRYPTION_KEY is not a valid Fernet key"
            ) from exc

    async def load_refresh_token(self) -> str | None:
        async with self.sessions() as session:
            token = await session.scalar(select(OAuthToken).where(OAuthToken.subject == "me"))
        if token is None:
            return self.settings.google_refresh_token
        try:
            return self._fernet().decrypt(token.encrypted_refresh_token.encode()).decode()
        except InvalidToken as exc:
            raise CredentialStoreError("Stored OAuth token cannot be decrypted") from exc

    async def save_refresh_token(self, refresh_token: str, scopes: str | None = None) -> None:
        encrypted = self._fernet().encrypt(refresh_token.encode()).decode()
        async with self.sessions.begin() as session:
            token = await session.scalar(select(OAuthToken).where(OAuthToken.subject == "me"))
            if token is None:
                session.add(
                    OAuthToken(subject="me", encrypted_refresh_token=encrypted, scopes=scopes)
                )
            else:
                token.encrypted_refresh_token = encrypted
                if scopes is not None:
                    token.scopes = scopes

    async def save_connection(
        self,
        refresh_token: str,
        scopes: str | None,
        health_user_id: str,
        legacy_user_id: str | None,
    ) -> None:
        """Activate a verified token and its identity in one transaction."""
        encrypted = self._fernet().encrypt(refresh_token.encode()).decode()
        async with self.sessions.begin() as session:
            token = await session.scalar(select(OAuthToken).where(OAuthToken.subject == "me"))
            if token is None:
                token = OAuthToken(subject="me", encrypted_refresh_token=encrypted)
                session.add(token)
            token.encrypted_refresh_token = encrypted
            if scopes is not None:
                token.scopes = scopes
            token.health_user_id = health_user_id
            token.legacy_user_id = legacy_user_id
