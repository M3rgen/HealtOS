"""0016 upgrade and atomic report writes on PostgreSQL; requires the external server."""

import copy
import uuid

import pytest
from alembic import command
from reconciliation_records import END, START, Client, Frozen, execute, point
from retrieval_records import Runner
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_pg_migrations import HEAD

from app.core.timeutil import as_utc
from app.google_health.client import Page
from app.models.raw_versions import RawHealthDataVersion
from app.models.retrieval import RetrievalCheckpoint, RetrievalReceipt
from app.models.state import RawHealthData
from app.services.reconciliation_reports import recovery_errors
from app.sync import incremental, reconciliation

pytestmark = pytest.mark.integration


async def test_0016_upgrade_preserves_raw_versions_and_immutable_receipts(
    database_url, alembic_config, monkeypatch
):
    command.upgrade(alembic_config, "20261025_0015")
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(reconciliation, "datetime", Frozen)
    a, b, missing, new = [point(at=f"2026-09-01T{hour}:00:00Z") for hour in (10, 11, 12, 13)]
    changed = copy.deepcopy(b)
    changed["heartRate"]["beatsPerMinute"] = 78
    runner = Runner(sessions, "sync")
    try:
        await runner.run(Client(Page([a, b, missing], None)))
        command.upgrade(alembic_config, "head")
        command.check(alembic_config)
        row, _, _ = await execute(sessions, Page([a, changed, new], None))
        assert row["status"] == "completed" and isinstance(row["id"], uuid.UUID)
        assert (as_utc(row["requested_from"]), as_utc(row["requested_to"])) == (START, END)
        assert [
            row["types"][0][n] for n in ("inserted", "updated", "unchanged", "source_deleted")
        ] == [1, 1, 1, 1]
        async with sessions() as session:
            raw = list((await session.scalars(select(RawHealthData))).all())
            assert len(raw) == 4
            assert next(r for r in raw if r.raw_json == missing).source_deleted_at is not None
            versions = list((await session.scalars(select(RawHealthDataVersion))).all())
            assert len(versions) == 1 and versions[0].raw_json == b
            assert await session.scalar(select(func.count()).select_from(RetrievalReceipt)) == 2
        assert await recovery_errors(sessions, row["id"]) == ()
        with pytest.raises(RuntimeError, match="holds reports; downgrade refused"):
            command.downgrade(alembic_config, "20261025_0015")
        sync_engine = create_engine(database_url.replace("+asyncpg", "+psycopg"))
        try:
            with sync_engine.connect() as connection:
                # env.py runs the whole command in one transaction: 0016's refusal also
                # rolls back the later downgrades that ran before it (0020 -> 0016), so
                # the database stays at head with its tables and reports intact.
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
                assert connection.scalar(text("SELECT to_regclass('derived_days')")) is not None
                assert connection.scalar(text("SELECT count(*) FROM reconciliation_runs")) == 1
        finally:
            sync_engine.dispose()
    finally:
        await engine.dispose()


async def test_postgresql_rollback_preserves_only_previous_committed_page(pg_sessions, monkeypatch):
    monkeypatch.setattr(reconciliation, "datetime", Frozen)
    original = incremental.record_changes
    calls = 0

    async def fail_second(session, data_type, stats, **kwargs):
        nonlocal calls
        await original(session, data_type, stats, **kwargs)
        calls += 1
        if calls == 2:
            raise RuntimeError(
                "synthetic rollback after raw, receipt, checkpoint and report writes"
            )

    monkeypatch.setattr(incremental, "record_changes", fail_second)
    row, results, _ = await execute(pg_sessions, Page([point()], "second"), Page([point()], None))
    assert isinstance(results["heart-rate"], RuntimeError)
    assert row["status"] == "partial" and row["types"][0]["inserted"] == 1
    assert row["types"][0]["pages_committed"] == 1
    assert await recovery_errors(pg_sessions, row["id"]) == ("RuntimeError",)
    async with pg_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 1
        checkpoint = await session.get(RetrievalCheckpoint, ("sync", "heart-rate"))
        assert checkpoint.next_token_hash is not None
