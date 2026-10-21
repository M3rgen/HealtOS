from __future__ import annotations

import os
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.db.base import Base

# A developer .env with APP_UI_TOKEN must not put the shared test app behind the login;
# auth tests pass the token explicitly.
os.environ["APP_UI_TOKEN"] = ""


@pytest_asyncio.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def clock(monkeypatch):
    """store_records' wall clock and the database clock of the sightings (identity tests:
    sightings decide what was seen together)."""
    from identity_records import Clock

    from app.services import source_chains, storage

    current = Clock()

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003 - signature of datetime.now
            return current.now

    async def database_now(session):  # noqa: ARG001 - signature of the real one
        return current.now

    monkeypatch.setattr(storage, "datetime", Frozen)
    monkeypatch.setattr(source_chains, "database_now", database_now)
    return current
