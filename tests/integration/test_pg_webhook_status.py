"""Webhook diagnostic guards on real PostgreSQL; synthetic evidence only."""

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.state import WebhookEvent
from app.models.webhook_status import PG_TRIGGER_SQL, TABLE, WebhookStatusEvent
from app.services.raw_history import verify_guards

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 1, tzinfo=UTC)


@pytest_asyncio.fixture(params=["migration", "metadata"])
async def status_sessions(request, database_url, alembic_config):
    engine = create_async_engine(database_url)
    try:
        if request.param == "migration":
            command.upgrade(alembic_config, "head")
        else:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_guards_refuse_all_changes_in_origin_and_replica_sessions(status_sessions):
    async with status_sessions.begin() as session:
        session.add_all(
            WebhookStatusEvent(reason=reason, occurred_at=NOW)
            for reason in ("signature_valid", "invalid_signature")
        )
    async with status_sessions() as session:
        before = (await session.execute(text(f"SELECT * FROM {TABLE} ORDER BY id"))).all()
        assert len(before) == 2
        assert await verify_guards(session) == []
    for replica in (False, True):
        for statement in (
            f"UPDATE {TABLE} SET reason = 'challenge'",
            f"UPDATE {TABLE} SET occurred_at = '2000-01-01'",
            f"UPDATE {TABLE} SET id = id + 100",
            f"DELETE FROM {TABLE}",
            f"TRUNCATE {TABLE}",
        ):
            with pytest.raises(DBAPIError, match="keeps every record"):
                async with status_sessions.begin() as session:
                    if replica:
                        await session.execute(text("SET LOCAL session_replication_role = replica"))
                    await session.execute(text(statement))
            async with status_sessions() as session:
                assert (
                    await session.execute(text(f"SELECT * FROM {TABLE} ORDER BY id"))
                ).all() == before
    async with status_sessions.begin() as session:
        await session.execute(text("SET LOCAL session_replication_role = replica"))
        session.add(WebhookStatusEvent(reason="challenge", occurred_at=NOW))
    async with status_sessions() as session:
        assert await session.scalar(text(f"SELECT count(*) FROM {TABLE}")) == 3


@pytest.mark.parametrize("suffix", ["append_only", "no_truncate"])
@pytest.mark.parametrize("tampering", ["disabled", "ordinary", "replica", "missing", "redefined"])
async def test_verify_reports_missing_redefined_or_non_always_guards(
    pg_sessions, suffix, tampering
):
    trigger = f"{TABLE}_{suffix}"
    async with pg_sessions() as session:
        assert await verify_guards(session) == []
    async with pg_sessions.begin() as session:
        if tampering in ("missing", "redefined"):
            await session.execute(text(f"DROP TRIGGER {trigger} ON {TABLE}"))
            if tampering == "redefined":
                sql = PG_TRIGGER_SQL[trigger][1]
                sql = (
                    sql.replace("FOR EACH ROW\n", "FOR EACH ROW WHEN (false)\n")
                    if suffix == "append_only"
                    else sql.replace("BEFORE", "AFTER")
                )
                await session.execute(text(sql))
                await session.execute(text(f"ALTER TABLE {TABLE} ENABLE ALWAYS TRIGGER {trigger}"))
        else:
            mode = {"disabled": "DISABLE", "ordinary": "ENABLE", "replica": "ENABLE REPLICA"}
            await session.execute(text(f"ALTER TABLE {TABLE} {mode[tampering]} TRIGGER {trigger}"))
    async with pg_sessions() as session:
        errors = await verify_guards(session)
    expected = {
        "disabled": "is disabled",
        "ordinary": "is enabled (not ALWAYS)",
        "replica": "is replica-only",
        "missing": "missing:",
        "redefined": "was redefined",
    }[tampering]
    assert len(errors) == 1
    assert trigger in errors[0] and expected in errors[0]


def test_guard_migration_preserves_rows_other_guards_and_shared_function(
    database_url, alembic_config
):
    engine = create_engine(database_url.replace("+asyncpg", "+psycopg"))

    def snapshot():
        with engine.connect() as connection:
            rows = {
                table: connection.execute(text(f"SELECT * FROM {table} ORDER BY 1")).all()
                for table in ("webhook_events", TABLE)
            }
            guards = dict(
                connection.execute(
                    text(
                        "SELECT t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t "
                        "JOIN pg_class c ON c.oid = t.tgrelid "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE NOT t.tgisinternal AND n.nspname = current_schema()"
                    )
                ).all()
            )
            function = connection.scalar(
                text("SELECT pg_get_functiondef('healthos_refuse_change()'::regprocedure)")
            )
            return rows, guards, function

    try:
        command.upgrade(alembic_config, "20261025_0018")
        with Session(engine) as session, session.begin():
            session.add(
                WebhookEvent(
                    payload_hash="c" * 64,
                    raw_json=[{"data": {"operation": "UPSERT"}}],
                    raw_body=b' [ {"data": {"operation":"UPSERT"}} ]\n',
                    signature_verified_at=NOW,
                    last_error="synthetic preserved archive error",
                )
            )
            session.add_all(
                WebhookStatusEvent(reason=reason, occurred_at=NOW)
                for reason in ("invalid_signature", "signature_valid")
            )
        before, old_guards, function = snapshot()
        command.upgrade(alembic_config, "20261025_0019")
        after, guards, new_function = snapshot()
        assert after == before and new_function == function
        assert set(guards) - set(old_guards) == set(PG_TRIGGER_SQL)
        assert all(guards[name] == sql for name, sql in old_guards.items())
        command.upgrade(alembic_config, "head")
        command.check(alembic_config)
        command.downgrade(alembic_config, "20261025_0018")
        assert snapshot() == (before, old_guards, function)
        command.upgrade(alembic_config, "20261025_0019")
        assert snapshot() == (after, guards, function)
    finally:
        engine.dispose()
