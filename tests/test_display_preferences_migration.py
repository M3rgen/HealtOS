"""Migration 20261102_0022: adds `display_preferences` and touches nothing else (SQLite)."""

import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings

SOURCE = Path("migrations/versions/20261102_0022_display_preferences.py")


def test_drop_appears_only_in_downgrade():
    source = SOURCE.read_text(encoding="utf-8")
    upgrade, downgrade = source.split("def downgrade()")
    assert not re.search(r"drop|DROP", upgrade.split("def upgrade()")[1])
    assert "drop_table" in downgrade and "from app" not in source


def test_upgrade_downgrade_upgrade_keeps_other_rows(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'display.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    engine = create_engine(url)
    try:
        command.upgrade(config, "20261031_0021")
        with engine.begin() as connection:
            connection.execute(
                text(
                    'INSERT INTO appearance_settings (id, selection) VALUES (1, \'{"logo": "x"}\')'
                )
            )
        before = set(inspect(engine).get_table_names())
        command.upgrade(config, "20261102_0022")
        assert set(inspect(engine).get_table_names()) - before == {"display_preferences"}
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO display_preferences (id, schema_version, preferences) "
                    "VALUES (1, 1, '{\"motion\": false}')"
                )
            )
            # The singleton check refuses a second row.
            with (
                pytest.raises(IntegrityError, match="CHECK constraint failed"),
                connection.begin_nested(),
            ):
                connection.execute(
                    text(
                        "INSERT INTO display_preferences (id, schema_version, preferences) "
                        "VALUES (2, 1, '{}')"
                    )
                )
        # Later revisions (20261102_0023) add their own tables; drift is checked at head.
        command.upgrade(config, "head")
        command.check(config)
        command.downgrade(config, "20261102_0022")
        engine.dispose()
        command.downgrade(config, "20261031_0021")
        assert set(inspect(engine).get_table_names()) == before
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT selection FROM appearance_settings")) == (
                '{"logo": "x"}'
            )
        command.upgrade(config, "head")
        command.check(config)
    finally:
        engine.dispose()
        get_settings.cache_clear()
