"""PostgreSQL-only integration fixtures.

The tests in this directory run only when ``DATABASE_URL`` points at PostgreSQL (CI's
service container); otherwise they skip. Set ``HEALTHOS_REQUIRE_POSTGRES=1`` to turn the
skip into a failure, so a CI step cannot pass by silently skipping everything.

``DATABASE_URL`` is only used to reach the server: every test gets a dedicated, freshly
created database that is dropped afterwards, so the shared CI database (where the
workflow runs ``alembic upgrade head``/``check`` itself) is never touched.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings

# A server time zone other than UTC proves the application never relies on it.
SESSION_TIME_ZONE = "America/Los_Angeles"


def _server_url() -> URL:
    raw = os.environ.get("DATABASE_URL", "")
    url = make_url(raw) if raw else None
    if url is None or url.get_backend_name() != "postgresql":
        message = "integration tests need DATABASE_URL pointing at PostgreSQL"
        if os.environ.get("HEALTHOS_REQUIRE_POSTGRES") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return url


def _render(url: URL, driver: str) -> str:
    return url.set(drivername=f"postgresql+{driver}").render_as_string(hide_password=False)


@pytest.fixture
def database_url(monkeypatch) -> Iterator[str]:
    """Create a throwaway database; yields its asyncpg URL and points the app at it."""
    server = _server_url()
    name = f"healthos_it_{uuid.uuid4().hex[:12]}"
    admin = create_engine(_render(server, "psycopg"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        url = _render(server.set(database=name), "asyncpg")
        monkeypatch.setenv("DATABASE_URL", url)
        get_settings.cache_clear()
        yield url
    finally:
        get_settings.cache_clear()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def alembic_config(database_url) -> Iterator[Config]:
    # migrations/env.py reads DATABASE_URL through get_settings(), patched above. It also
    # runs logging.config.fileConfig(), which disables every existing logger; restore
    # them so later tests asserting on log records (caplog) are unaffected.
    manager = logging.Logger.manager
    loggers = [logging.getLogger()] + [
        item for item in manager.loggerDict.values() if isinstance(item, logging.Logger)
    ]
    saved = [
        (item, item.disabled, item.level, list(item.handlers), item.propagate) for item in loggers
    ]
    try:
        yield Config("alembic.ini")
    finally:
        for item, disabled, level, handlers, propagate in saved:
            item.disabled, item.level, item.propagate = disabled, level, propagate
            item.handlers = handlers
        # Loggers first created during the migration (alembic.*) are re-enabled too.
        for item in list(manager.loggerDict.values()):
            if isinstance(item, logging.Logger) and item not in loggers:
                item.disabled = False


@pytest.fixture
def migrated(alembic_config) -> Config:
    command.upgrade(alembic_config, "head")
    return alembic_config


@pytest_asyncio.fixture
async def pg_sessions(database_url, migrated) -> AsyncIterator[async_sessionmaker]:
    engine = create_async_engine(
        database_url,
        connect_args={"server_settings": {"TimeZone": SESSION_TIME_ZONE}},
    )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
