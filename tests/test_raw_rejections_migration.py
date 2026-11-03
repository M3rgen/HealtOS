"""20261102_0023 only adds guarded tables: nothing existing is altered, locked or rewritten."""

import re
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import raw_rejections
from app.models.raw_rejections import RawHealthDataRejection, RawRejectionAck
from app.models.raw_rejections import RetrievalEventRejection as EventRejection
from app.models.state import WebhookEvent

SCHEMA = text("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")


def _rejection(**values):
    return RawHealthDataRejection(
        data_type="steps",
        source="sync",
        response_id=None,
        ordinal=0,
        reason=raw_rejections.REASON,
        element_sha256="0" * 64,
        element_json=b'{"x": "\\u0000"}',
        **values,
    )


@pytest.fixture
def database(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'rejections.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = create_engine(url)
    try:
        yield Config("alembic.ini"), engine
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_upgrade_adds_only_new_tables_and_keeps_every_existing_row(database):
    config, engine = database
    command.upgrade(config, "20261102_0022")
    with Session(engine) as session, session.begin():
        session.add(
            WebhookEvent(
                payload_hash="d" * 64,
                raw_json={"synthetic": True},
                raw_body=b' {"synthetic":true}\n',
                signature_verified_at=datetime(2026, 11, 1, tzinfo=UTC),
            )
        )
    with engine.connect() as connection:
        schema = set(connection.execute(SCHEMA).tuples())
        rows = connection.execute(text("SELECT * FROM webhook_events")).all()
    tables = set(inspect(engine).get_table_names())
    command.upgrade(config, "20261102_0023")
    command.check(config)
    assert set(inspect(engine).get_table_names()) - tables == set(raw_rejections.TABLES)
    with engine.connect() as connection:
        after = set(connection.execute(SCHEMA).tuples())
        assert connection.execute(text("SELECT * FROM webhook_events")).all() == rows
    # Every existing table, index and trigger is defined exactly as before.
    assert schema <= after
    added = {name for _, name, _ in after - schema}
    assert {name for name, (table, _) in raw_rejections.SQLITE_TRIGGER_SQL.items()} <= added
    assert all(any(table in name for table in raw_rejections.TABLES) for name in added), added


@pytest.mark.parametrize("table", raw_rejections.TABLES)
def test_rows_refuse_change_and_block_downgrade_until_empty(database, table):
    config, engine = database
    command.upgrade(config, "head")
    row = {
        raw_rejections.REJECTIONS: lambda: _rejection(),
        raw_rejections.ACKS: lambda: RawRejectionAck(through_id=1),
        raw_rejections.EVENT_COUNTS: lambda: EventRejection(event_id=7, rejected_count=2),
    }[table]()
    with Session(engine) as session, session.begin():
        session.add(row)
    for statement in (f"UPDATE {table} SET rowid = rowid + 100", f"DELETE FROM {table}"):
        with pytest.raises(DBAPIError, match="refused"), engine.begin() as connection:
            connection.execute(text(statement))
    with pytest.raises(RuntimeError, match=f"{table} holds evidence; downgrade refused"):
        command.downgrade(config, "20261102_0022")
    with engine.connect() as connection:
        assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1


def test_empty_tables_downgrade_and_upgrade_again(database):
    config, engine = database
    command.upgrade(config, "head")
    command.downgrade(config, "20261102_0022")
    assert not set(raw_rejections.TABLES) & set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        names = {name for _, name, _ in connection.execute(SCHEMA).tuples()}
    assert not any(table in name for name in names for table in raw_rejections.TABLES)
    command.upgrade(config, "head")
    command.check(config)


def test_postgresql_ddl_creates_no_foreign_key_and_alters_only_its_own_tables(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://health:health@postgres:5432/health")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "20261102_0022:20261102_0023", sql=True)
        ddl = capsys.readouterr().out
        with pytest.raises(RuntimeError, match="online downgrade"):
            command.downgrade(Config("alembic.ini"), "20261102_0023:20261102_0022", sql=True)
    finally:
        get_settings.cache_clear()
    assert "REFERENCES" not in ddl and "LOCK TABLE" not in ddl
    assert ddl.count("CREATE TABLE ") == 3
    altered = set(re.findall(r"ALTER TABLE (\w+) ENABLE ALWAYS TRIGGER", ddl))
    assert altered == set(raw_rejections.TABLES)
    assert len(re.findall(r"ALTER TABLE", ddl)) == 6
    targets = re.findall(r"(?:CREATE (?:UNIQUE )?INDEX \w+|BEFORE [A-Z ]+?) ON (\w+)", ddl)
    assert len(targets) == 2 + 6 and set(targets) == set(raw_rejections.TABLES)
    assert "WHERE response_id IS NULL" in ddl
