from pathlib import Path

from alembic import command
from alembic.config import Config

from app.core.config import get_settings


def test_initial_revision_is_frozen() -> None:
    source = Path("migrations/versions/20260818_0001_initial.py").read_text(encoding="utf-8")
    assert "app.models" not in source
    assert "Base.metadata" not in source
    assert ".create_all(" not in source


def test_migration_upgrade_drift_downgrade_cycle(tmp_path, monkeypatch) -> None:
    database = (tmp_path / "migration.db").as_posix()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")
    get_settings.cache_clear()
    config = Config("alembic.ini")
    try:
        command.upgrade(config, "head")
        command.check(config)
        command.downgrade(config, "base")
        command.upgrade(config, "head")
    finally:
        get_settings.cache_clear()


def test_postgresql_offline_ddl_uses_native_types(monkeypatch, capsys) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://health:health@postgres:5432/health",
    )
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "head", sql=True)
        ddl = capsys.readouterr().out
    finally:
        get_settings.cache_clear()
    assert "CREATE TYPE jobstatus AS ENUM" in ddl
    assert "id UUID NOT NULL" in ddl
    assert "TIMESTAMP WITH TIME ZONE" in ddl
    assert ddl.count("CREATE TABLE ") == 39  # 38 application tables + Alembic version.
