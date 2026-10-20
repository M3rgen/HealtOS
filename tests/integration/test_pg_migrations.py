"""Alembic history against a real PostgreSQL server (autocommit blocks, enum, index)."""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

FRESHNESS_INDEX = "ix_raw_health_data_type_freshness"
AD_HOC_INDEX = "ix_raw_health_data_type_effective_time"
HEAD = "20261020_0011"


def _sync_url(database_url: str) -> str:
    return database_url.replace("+asyncpg", "+psycopg")


def _state(engine) -> tuple[str, set[str], str | None]:
    with engine.connect() as connection:
        version = connection.scalar(text("SELECT version_num FROM alembic_version"))
        statuses = set(
            connection.scalars(text("SELECT unnest(enum_range(NULL::jobstatus))::text")).all()
        )
        index = connection.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'raw_health_data' AND indexname = :name"
            ),
            {"name": FRESHNESS_INDEX},
        )
    return version, statuses, index


def test_fresh_upgrade_downgrade_and_reupgrade(database_url, alembic_config) -> None:
    engine = create_engine(_sync_url(database_url))
    try:
        # Stop before 0007 and recreate the hand-made index it is meant to replace.
        command.upgrade(alembic_config, "20261001_0006")
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"CREATE INDEX {AD_HOC_INDEX} ON raw_health_data "
                    "(data_type, (CASE WHEN source_end_time IS NULL "
                    "THEN source_start_time ELSE source_end_time END))"
                )
            )

        command.upgrade(alembic_config, "head")
        version, statuses, index = _state(engine)
        assert version == HEAD
        assert "abandoned" in statuses
        assert index is not None
        assert "COALESCE(source_end_time, source_start_time)" in index
        with engine.connect() as connection:
            indexes = set(
                connection.scalars(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = 'raw_health_data'")
                ).all()
            )
            # CREATE INDEX CONCURRENTLY leaves an INVALID index behind if it fails.
            invalid = connection.scalar(text("SELECT count(*) FROM pg_index WHERE NOT indisvalid"))
        assert AD_HOC_INDEX not in indexes
        assert invalid == 0
        # The ORM metadata (incl. the expression index) matches the migrated schema.
        command.check(alembic_config)

        # The new enum value is usable after upgrade; downgrading parks it as failed.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO backfill_jobs (id, data_type, window_start, window_end, status, "
                    "records_downloaded, retry_count, created_at, updated_at) VALUES "
                    "(gen_random_uuid(), 'steps', '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z', "
                    "'abandoned', 0, 10, now(), now())"
                )
            )
        command.downgrade(alembic_config, "20261017_0007")
        version, statuses, index = _state(engine)
        assert version == "20261017_0007"
        assert index is not None
        with engine.connect() as connection:
            job_status = connection.scalar(text("SELECT status::text FROM backfill_jobs"))
        assert job_status == "failed"

        command.upgrade(alembic_config, "head")
        assert _state(engine)[0] == HEAD

        # Back past 0007 drops the freshness index concurrently; head restores it.
        command.downgrade(alembic_config, "20261001_0006")
        version, _, index = _state(engine)
        assert (version, index) == ("20261001_0006", None)
        command.upgrade(alembic_config, "head")
        version, statuses, index = _state(engine)
        assert version == HEAD
        assert "abandoned" in statuses
        assert index is not None
        command.check(alembic_config)
    finally:
        engine.dispose()


def test_0011_gives_up_on_a_held_lock_instead_of_blocking(
    database_url, alembic_config, monkeypatch
) -> None:
    from argparse import Namespace

    command.upgrade(alembic_config, "20261020_0010")
    # Safety net for the test only: without the migration's own lock_timeout the upgrade
    # would wait forever; this turns that into a failure after 20 s.
    monkeypatch.setenv("PGOPTIONS", "-c statement_timeout=20000")
    engine = create_engine(_sync_url(database_url))
    blocker = engine.connect()
    try:
        # What pg_dump holds on every table for the length of the dump.
        blocker.execute(text("LOCK TABLE raw_health_data IN ACCESS SHARE MODE"))
        alembic_config.cmd_opts = Namespace(x=["lock-timeout=1s"])
        with pytest.raises(RuntimeError, match="could not lock its tables within 1s.*retry"):
            command.upgrade(alembic_config, "head")
        blocker.rollback()
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20261020_0010"
            )
            assert connection.scalar(text("SELECT to_regclass('raw_api_responses')")) is None
        monkeypatch.delenv("PGOPTIONS")
        alembic_config.cmd_opts = None
        command.upgrade(alembic_config, "head")  # once the lock is free, it just works
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
    finally:
        blocker.close()
        engine.dispose()
