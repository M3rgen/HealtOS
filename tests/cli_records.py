"""A file-backed archive for synchronous CLI invocations on separate event loops."""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.cli import main as cli
from app.db.base import Base


@pytest.fixture
def cli_database(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}", poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def prepare():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(prepare())
    monkeypatch.setattr(cli, "SessionLocal", sessions)
    try:
        yield sessions
    finally:
        asyncio.run(engine.dispose())
