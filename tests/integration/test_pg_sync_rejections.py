"""Rejected records against real PostgreSQL jsonb (see test_sync_rejections), and 0023's locks."""

from argparse import Namespace

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from test_pg_migrations import HEAD
from test_sync_rejections import KINDS
from test_sync_rejections import (
    test_a_failed_page_rolls_back_its_rejections_but_keeps_the_exchange as check_rollback,
)
from test_sync_rejections import (
    test_a_rejected_record_defers_source_deletion_until_a_clean_round as check_deferral,
)
from test_sync_rejections import (
    test_an_unstorable_record_no_longer_blocks_its_data_type as check_unblocked,
)
from test_sync_rejections import (
    test_every_exchange_keeps_its_own_response_and_rejection_row as check_exchanges,
)
from test_sync_rejections import (
    test_without_an_exchange_the_record_key_prevents_repeats as check_unanchored,
)

pytestmark = pytest.mark.integration
PREVIOUS = "20261102_0022"


@pytest.mark.parametrize("kind", KINDS)
async def test_postgres_rejection_unblocks_the_data_type(pg_sessions, monkeypatch, kind, caplog):
    await check_unblocked(pg_sessions, monkeypatch, kind, caplog)


async def test_postgres_keeps_every_exchange(pg_sessions):
    await check_exchanges(pg_sessions)


async def test_postgres_defers_source_deletion(pg_sessions):
    await check_deferral(pg_sessions)


async def test_postgres_rolls_back_rejections_with_their_page(pg_sessions, monkeypatch):
    await check_rollback(pg_sessions, monkeypatch)


async def test_postgres_partial_key_prevents_unanchored_repeats(pg_sessions):
    await check_unanchored(pg_sessions)


def test_0023_runs_next_to_a_dump(database_url, alembic_config, monkeypatch):
    command.upgrade(alembic_config, PREVIOUS)
    # Safety net for the test only: a wait for a lock fails after 20 s instead of hanging.
    monkeypatch.setenv("PGOPTIONS", "-c statement_timeout=20000")
    engine = create_engine(database_url.replace("+asyncpg", "+psycopg"))
    blocker = engine.connect()
    try:
        tables = blocker.scalars(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
        ).all()
        # What pg_dump holds on every table for the length of the dump.
        blocker.execute(text(f"LOCK TABLE {', '.join(tables)} IN ACCESS SHARE MODE"))
        alembic_config.cmd_opts = Namespace(x=[])
        command.upgrade(alembic_config, HEAD)
        blocker.rollback()
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
    finally:
        blocker.close()
        engine.dispose()
