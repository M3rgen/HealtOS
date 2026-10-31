"""Migration 20261031_0021: body-only webhook rows, frozen SQL, refused downgrade (SQLite).

tests/integration/test_pg_webhook_body_only_migration.py covers PostgreSQL.
"""

import importlib.util
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, create_engine, text

from app.core.config import get_settings
from app.db.base import Base
from app.models.raw_versions import SQLITE_TRIGGER_SQL


def test_sqlite_migration_keeps_guards_and_refuses_downgrade_over_null_rows(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'unstorable.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    engine = create_engine(url)
    try:
        command.upgrade(config, "head")
        command.check(config)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO webhook_events (payload_hash, raw_body, element_count, "
                    "failure_count, created_at, updated_at) "
                    "VALUES ('b', x'00', 0, 0, '2026-10-01', '2026-10-01')"
                )
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM sqlite_master WHERE type = 'trigger' "
                        "AND tbl_name IN ('webhook_events', 'webhook_notifications')"
                    )
                )
                == 4
            )
        with pytest.raises(RuntimeError, match="body-only rows; downgrade refused"):
            command.downgrade(config, "20261025_0020")
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT raw_json, raw_body FROM webhook_events")
            ).all() == [(None, b"\x00")]
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_migration_sql_is_frozen_and_matches_metadata():
    path = Path("migrations/versions/20261031_0021_webhook_body_only.py")
    spec = importlib.util.spec_from_file_location("webhook_body_only_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert (migration.revision, migration.down_revision) == ("20261031_0021", "20261025_0020")
    assert migration.SQLITE_TRIGGERS == tuple(
        sql for table, sql in SQLITE_TRIGGER_SQL.values() if table in migration.CHECKS
    )
    for table, (name, condition) in migration.CHECKS.items():
        checks = Base.metadata.tables[table].constraints
        assert [
            str(c.sqltext)
            for c in checks
            if isinstance(c, CheckConstraint) and c.name == f"ck_{table}_{name}"
        ] == [condition]
    assert "app." not in path.read_text()
