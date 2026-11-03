"""0018 adds a disposable read model without altering any protected archive object."""

import importlib.util
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import derived_invalidation


def _migration(suffix):
    path = next(Path("migrations/versions").glob(f"20261025_{suffix}_*.py"))
    spec = importlib.util.spec_from_file_location(f"migration{suffix}", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_historical_0018_contract_and_new_head_match_metadata():
    old, new = _migration("0018"), _migration("0020")
    assert new.revision == "20261025_0020" and new.down_revision == "20261025_0019"
    replacements = dict(zip(new.PREVIOUS_POSTGRESQL_DDL, new.POSTGRESQL_DDL, strict=True))
    # The historical SQL must still match 0018, including the former WHEN clauses.
    # Only the four explicitly replaced triggers differ at the current head.
    historical = tuple(
        sql.replace("CREATE TRIGGER", "CREATE OR REPLACE TRIGGER") for sql in old.POSTGRESQL_DDL
    )
    assert set(new.PREVIOUS_POSTGRESQL_DDL) <= set(historical)
    upgraded = tuple(
        replacements.get(sql, sql).replace("CREATE OR REPLACE TRIGGER", "CREATE TRIGGER")
        for sql in historical
    )
    assert upgraded == derived_invalidation.POSTGRESQL_DDL
    assert old.SQLITE_DDL == derived_invalidation.SQLITE_DDL
    assert len(new.POSTGRESQL_DDL) == 8
    assert all(
        sql.startswith(("CREATE OR REPLACE TRIGGER ", "ALTER TABLE ")) for sql in new.POSTGRESQL_DDL
    )
    source = Path(new.__file__).read_text()
    assert "from app" not in source and "import app" not in source and "DROP" not in source
    for name, (_, sql) in derived_invalidation.PG_TRIGGER_SQL.items():
        if name.endswith("_insert"):
            assert "OLD." not in sql
    update_sql = derived_invalidation.PG_TRIGGER_SQL["derived_raw_health_data_update"][1]
    assert "OLD.data_type IN" in update_sql and "OR NEW.data_type IN" in update_sql


async def test_upgrade_and_downgrade_preserve_all_existing_triggers(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'derived.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = create_engine(url)
    async_engine = create_async_engine(url.replace("sqlite:", "sqlite+aiosqlite:"))
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "20261025_0017")
        with engine.connect() as connection:
            before = dict(
                connection.execute(
                    text("SELECT name, sql FROM sqlite_master WHERE type='trigger'")
                ).all()
            )
        command.upgrade(config, "20261025_0018")
        with engine.connect() as connection:
            after = dict(
                connection.execute(
                    text("SELECT name, sql FROM sqlite_master WHERE type='trigger'")
                ).all()
            )
        assert all(after[name] == sql for name, sql in before.items())
        assert set(after) - set(before) == set(derived_invalidation.SQLITE_TRIGGER_SQL)
        command.upgrade(config, "20261025_0019")
        from derived_records import T0, manual, raw_mutation, snapshot, steps, store

        from app.models import raw_rejections
        from app.models.state import WebhookEvent
        from app.models.webhook_status import WebhookStatusEvent
        from app.services.manual_flags import record_flag
        from app.services.raw_history import verify_guards

        await store(sessions, "steps", steps(count=100))
        await store(sessions, "steps", steps(count=120))
        mutation = await raw_mutation(sessions, "heart-rate", "device")
        identifier = await manual(sessions, 80)
        async with sessions.begin() as session:
            await session.execute(mutation)
            await record_flag(session, identifier, "test", True)
            session.add(
                WebhookEvent(
                    payload_hash="c" * 64,
                    raw_json={"synthetic": True},
                    raw_body=b"{}\n",
                    signature_verified_at=T0,
                )
            )
            session.add(WebhookStatusEvent(reason="signature_valid", occurred_at=T0))
        # Tables of later migrations (20261102_0022, 0023) do not exist at the 0019 target.
        newer = {"display_preferences", *raw_rejections.TABLES}
        # 0023's guards are expected at head only; verify names them missing at 0019.
        later = {name: sql for name, (_, sql) in raw_rejections.SQLITE_TRIGGER_SQL.items()}
        at_0019 = [
            f"protective trigger missing: {table}.{name}"
            for name, (table, _) in raw_rejections.SQLITE_TRIGGER_SQL.items()
        ]
        async with sessions() as session:
            archive = await snapshot(session, newer)
        # Sorted: 0021 rebuilds both webhook tables on SQLite, moving their triggers last.
        triggers = text("SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name")
        with engine.connect() as connection:
            legacy = connection.execute(triggers).all()
        for revision in ("head", "20261025_0019", "head"):
            if revision == "20261025_0019":
                command.downgrade(config, revision)
            else:
                command.upgrade(config, revision)
            async with sessions() as session:
                assert await snapshot(session, newer) == archive
                assert await verify_guards(session) == (
                    at_0019 if revision == "20261025_0019" else []
                )
            with engine.connect() as connection:
                stored = connection.execute(triggers).all()
            if revision == "head":
                assert sorted(stored) == sorted([*legacy, *later.items()])
                assert [row for row in stored if row[0] not in later] == legacy
            else:
                assert stored == legacy
        command.check(config)
        command.downgrade(config, "20261025_0017")
        with engine.connect() as connection:
            assert (
                dict(
                    connection.execute(
                        text("SELECT name, sql FROM sqlite_master WHERE type='trigger'")
                    ).all()
                )
                == before
            )
    finally:
        await async_engine.dispose()
        engine.dispose()
        get_settings.cache_clear()
