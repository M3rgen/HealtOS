"""Migration 20261031_0021 against real PostgreSQL: rows kept, guarded downgrade, locks."""

from argparse import Namespace

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from test_pg_migrations import HEAD

pytestmark = pytest.mark.integration
PREVIOUS = "20261025_0020"
TABLES = ("webhook_events", "webhook_notifications")


def _schema(connection):
    nullable = connection.execute(
        text(
            "SELECT table_name, is_nullable FROM information_schema.columns "
            "WHERE column_name = 'raw_json' AND table_name = ANY(:tables) ORDER BY 1"
        ),
        {"tables": list(TABLES)},
    ).all()
    checks = connection.scalars(
        text("SELECT conname FROM pg_constraint WHERE contype = 'c' AND conname LIKE 'ck_webhook%'")
    ).all()
    return nullable, sorted(checks)


def _archive(connection):
    return [connection.execute(text(f"SELECT * FROM {table} ORDER BY 1")).all() for table in TABLES]


def test_0021_keeps_rows_and_refuses_downgrade_over_body_only_rows(database_url, alembic_config):
    engine = create_engine(database_url.replace("+asyncpg", "+psycopg"))
    try:
        command.upgrade(alembic_config, PREVIOUS)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO webhook_events (payload_hash, raw_json, raw_body, element_count, "
                    "failure_count) VALUES ('a', '{\"data\": {}}', 'x'::bytea, 1, 0);"
                    "INSERT INTO webhook_notifications (element_hash, raw_json, is_valid, status, "
                    "attempt_count, sync_attempt_count, sync_success_count) "
                    "VALUES ('n', '{\"data\": {}}', false, 'rejected', 0, 0, 0)"
                )
            )
            archived = _archive(connection)
        command.upgrade(alembic_config, "head")
        command.check(alembic_config)
        with engine.connect() as connection:
            assert _schema(connection) == (
                [("webhook_events", "YES"), ("webhook_notifications", "YES")],
                ["ck_webhook_events_raw_kept", "ck_webhook_notifications_raw_or_rejected"],
            )
            assert _archive(connection) == archived
        command.downgrade(alembic_config, PREVIOUS)
        with engine.connect() as connection:
            assert _schema(connection) == (
                [("webhook_events", "NO"), ("webhook_notifications", "NO")],
                [],
            )
            assert _archive(connection) == archived

        command.upgrade(alembic_config, "head")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO webhook_events (payload_hash, raw_body, element_count, "
                    "failure_count) VALUES ('b', '\\x00'::bytea, 1, 1);"
                    "INSERT INTO webhook_notifications (element_hash, is_valid, status, "
                    "attempt_count, sync_attempt_count, sync_success_count) "
                    "VALUES ('m', false, 'rejected', 0, 0, 0)"
                )
            )
            archived = _archive(connection)
        with pytest.raises(RuntimeError, match="body-only rows; downgrade refused"):
            command.downgrade(alembic_config, PREVIOUS)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
            assert _archive(connection) == archived
        with pytest.raises(RuntimeError, match="checked before an online downgrade"):
            command.downgrade(alembic_config, f"{HEAD}:{PREVIOUS}", sql=True)
    finally:
        engine.dispose()


def test_0021_gives_up_next_to_a_dump_changing_nothing(database_url, alembic_config, monkeypatch):
    command.upgrade(alembic_config, PREVIOUS)
    # Safety net for the test only: a wait without lock_timeout fails after 20 s.
    monkeypatch.setenv("PGOPTIONS", "-c statement_timeout=20000")
    engine = create_engine(database_url.replace("+asyncpg", "+psycopg"))
    blocker = engine.connect()
    try:
        # What pg_dump holds on every table for the length of the dump.
        blocker.execute(text(f"LOCK TABLE {', '.join(TABLES)} IN ACCESS SHARE MODE"))
        alembic_config.cmd_opts = Namespace(x=["lock-timeout=1s"])
        with pytest.raises(RuntimeError, match="0021 could not lock .* within 1s.*retry"):
            command.upgrade(alembic_config, "head")
        blocker.rollback()
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == PREVIOUS
            assert _schema(connection)[1] == []
        monkeypatch.delenv("PGOPTIONS")
        alembic_config.cmd_opts = None
        command.upgrade(alembic_config, "head")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
    finally:
        blocker.close()
        engine.dispose()
