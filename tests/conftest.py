from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.db.base import Base

# A developer .env with APP_UI_TOKEN must not put the shared test app behind the login;
# auth tests pass the token explicitly.
os.environ["APP_UI_TOKEN"] = ""


@pytest.fixture(autouse=True)
def preserved_loggers() -> Iterator[None]:
    """Undo alembic's logging setup after every test that runs a migration.

    migrations/env.py runs logging.config.fileConfig(), which disables every existing
    logger and replaces the root handlers; later caplog tests would then see no records.
    Snapshotting costs microseconds, so every test (unit or integration) is covered.
    """
    manager = logging.Logger.manager
    loggers = [logging.getLogger()] + [
        item for item in manager.loggerDict.values() if isinstance(item, logging.Logger)
    ]
    saved = [
        (item, item.disabled, item.level, list(item.handlers), item.propagate) for item in loggers
    ]
    try:
        yield
    finally:
        for item, disabled, level, handlers, propagate in saved:
            item.disabled, item.level, item.propagate = disabled, level, propagate
            item.handlers = handlers
        # Loggers first created during the migration (alembic.*) are re-enabled too.
        for item in list(manager.loggerDict.values()):
            if isinstance(item, logging.Logger) and item not in loggers:
                item.disabled = False


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


@pytest_asyncio.fixture
async def dashboard_dates(sessions, monkeypatch):
    """Production routes, boundary archive, SQL trace and a read-only snapshot."""
    from datetime import UTC, date, timedelta

    import httpx
    from derived_records import manual, sleep, snapshot, steps, store, summary
    from fastapi import FastAPI
    from sqlalchemy import event
    from test_workout_corrections import exercise

    import app.api.dashboard as api
    import app.services.dashboard as service
    from app.db.session import get_session
    from app.models.manual import ManualWorkout

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(9999, 12, 31, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(api, "datetime", Fixed)
    monkeypatch.setattr(service, "datetime", Fixed)
    start = datetime(1, 1, 1, tzinfo=UTC)
    await store(sessions, "steps", steps(start=start, count=120))
    await store(sessions, "steps", steps(start=start, count=240))
    await store(sessions, "sleep", sleep(start=start, end=start + timedelta(hours=1)))
    await store(
        sessions,
        "daily-resting-heart-rate",
        summary("daily-resting-heart-rate", day=date.min, beatsPerMinute=71),
    )
    await store(sessions, "exercise", exercise(start=start))
    await manual(sessions, 70)
    async with sessions.begin() as session:
        session.add(
            ManualWorkout(
                exercise_type="Yürüyüş", start_time=start, end_time=start + timedelta(hours=1)
            )
        )
    async with sessions() as session:
        before = await snapshot(session)

    application = FastAPI()
    application.include_router(api.router)

    async def override_session():
        async with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    statements = []

    def trace(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = sessions.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", trace)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url="http://test"
    ) as client:
        yield client, statements
    event.remove(engine, "before_cursor_execute", trace)
    async with sessions() as session:
        assert await snapshot(session) == before
