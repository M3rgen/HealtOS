"""Additive diagnostics migration preserves existing webhook evidence byte for byte."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import raw_rejections, webhook_status
from app.models.state import WebhookEvent
from app.models.webhook_status import WebhookStatusEvent


def test_upgrade_keeps_raw_archive_and_populated_diagnostics_refuse_downgrade(
    tmp_path, monkeypatch
):
    url = f"sqlite:///{tmp_path / 'webhook-migration.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    engine = create_engine(url)
    now = datetime(2026, 10, 1, tzinfo=UTC)
    body = b' [ {"data": {"operation":"UPSERT"}} ]\n'
    try:
        command.upgrade(config, "20261025_0016")
        before = set(inspect(engine).get_table_names())
        with Session(engine) as session, session.begin():
            session.add(
                WebhookEvent(
                    payload_hash="a" * 64,
                    raw_json=[{"data": {"operation": "UPSERT"}}],
                    raw_body=body,
                    signature_verified_at=now,
                    last_error="synthetic preserved archive error",
                )
            )
        with engine.connect() as connection:
            archived = connection.execute(text("SELECT * FROM webhook_events")).all()
        command.upgrade(config, "head")
        assert set(inspect(engine).get_table_names()) - before == {
            "webhook_status_events",
            "derived_days",
            "display_preferences",
            *raw_rejections.TABLES,
        }
        command.check(config)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT * FROM webhook_events")).all() == archived
            assert connection.scalar(select(WebhookStatusEvent.id)) is None
        with Session(engine) as session, session.begin():
            session.add(WebhookStatusEvent(reason="signature_valid", occurred_at=now))
        with pytest.raises(RuntimeError, match="holds evidence; downgrade refused"):
            command.downgrade(config, "20261025_0016")
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "20261025_0017"
            )
            assert connection.execute(text("SELECT * FROM webhook_events")).all() == archived
            assert connection.scalar(text("SELECT count(*) FROM webhook_status_events")) == 1
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_guard_sql_is_frozen_and_matches_metadata():
    path = Path("migrations/versions/20261025_0019_webhook_status_guards.py")
    spec = importlib.util.spec_from_file_location("webhook_guard_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.revision == "20261025_0019"
    assert migration.down_revision == "20261025_0018"
    assert migration.POSTGRESQL_DDL == webhook_status.POSTGRESQL_DDL
    assert migration.SQLITE_DDL == webhook_status.SQLITE_DDL
    assert "app.models" not in path.read_text()
    assert "app.services" not in path.read_text()


def test_guard_upgrade_and_downgrade_preserve_populated_evidence(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'webhook-guards.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = create_engine(url)
    config = Config("alembic.ini")

    def snapshot():
        with engine.connect() as connection:
            return {
                table: connection.execute(text(f"SELECT * FROM {table} ORDER BY 1")).all()
                for table in ("webhook_events", "webhook_status_events")
            }

    def triggers():
        with engine.connect() as connection:
            return dict(
                connection.execute(
                    text("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")
                ).all()
            )

    try:
        command.upgrade(config, "20261025_0018")
        with Session(engine) as session, session.begin():
            session.add(
                WebhookEvent(
                    payload_hash="b" * 64,
                    raw_json=[{"data": {"operation": "UPSERT"}}],
                    raw_body=b' [ {"data": {"operation":"UPSERT"}} ]\n',
                    signature_verified_at=datetime(2026, 10, 1, tzinfo=UTC),
                    last_error="synthetic preserved archive error",
                )
            )
            session.add_all(
                WebhookStatusEvent(reason=reason, occurred_at=datetime(2026, 10, 1, tzinfo=UTC))
                for reason in ("invalid_signature", "signature_valid")
            )
        before, old_triggers = snapshot(), triggers()
        command.upgrade(config, "head")
        command.check(config)
        assert snapshot() == before
        assert triggers() == {
            **old_triggers,
            **{name: sql for name, (_, sql) in webhook_status.SQLITE_TRIGGER_SQL.items()},
            **{name: sql for name, (_, sql) in raw_rejections.SQLITE_TRIGGER_SQL.items()},
        }
        for statement in (
            "UPDATE webhook_status_events SET reason = 'challenge'",
            "UPDATE webhook_status_events SET occurred_at = '2000-01-01'",
            "UPDATE webhook_status_events SET id = id + 100",
            "DELETE FROM webhook_status_events",
        ):
            with pytest.raises(DBAPIError, match="keeps every record"):
                with engine.begin() as connection:
                    connection.execute(text(statement))
            assert snapshot() == before
        with Session(engine) as session, session.begin():
            session.add(
                WebhookStatusEvent(
                    reason="challenge", occurred_at=datetime(2026, 10, 2, tzinfo=UTC)
                )
            )
        after_insert = snapshot()
        assert after_insert["webhook_status_events"][:-1] == before["webhook_status_events"]
        assert len(after_insert["webhook_status_events"]) == 3
        command.downgrade(config, "20261025_0018")
        assert snapshot() == after_insert
        assert triggers() == old_triggers
        command.upgrade(config, "head")
        assert snapshot() == after_insert
        assert snapshot()["webhook_events"] == before["webhook_events"]
    finally:
        engine.dispose()
        get_settings.cache_clear()
