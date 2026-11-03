"""Additive schema, downgrade retention and database-level report constraints."""

import uuid

import pytest
from alembic import command
from alembic.config import Config
from reconciliation_records import END, START
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import raw_rejections
from app.models.reconciliation import ReconciliationResult, ReconciliationRun
from app.services import reconciliation_reports as reports


def test_populated_report_prevents_downgrade_and_survives_upgrade_check(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'reports.sqlite'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    engine = create_engine(url)
    try:
        command.upgrade(config, "20261025_0015")
        before = set(inspect(engine).get_table_names())
        command.upgrade(config, "head")
        assert set(inspect(engine).get_table_names()) - before == {
            "reconciliation_runs",
            "reconciliation_results",
            "webhook_status_events",
            "derived_days",
            "display_preferences",
            *raw_rejections.TABLES,
        }
        with Session(engine) as session, session.begin():
            run_id = uuid.uuid4()
            session.add(
                ReconciliationRun(
                    id=run_id, requested_from=START, requested_to=END, started_at=START
                )
            )
            session.flush()
            session.add(ReconciliationResult(run_id=run_id, data_type="steps"))
        with pytest.raises(RuntimeError, match="holds reports; downgrade refused"):
            command.downgrade(config, "20261025_0015")
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "20261025_0016"
            )
            assert connection.scalar(text("SELECT count(*) FROM reconciliation_runs")) == 1
            assert connection.scalar(text("SELECT count(*) FROM reconciliation_results")) == 1
        command.upgrade(config, "head")
        command.check(config)
    finally:
        engine.dispose()
        get_settings.cache_clear()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE reconciliation_results SET inserted = -1",
        "UPDATE reconciliation_results SET source_deleted = -1",
        "UPDATE reconciliation_results SET pages_committed = -1",
        "UPDATE reconciliation_results SET status = 'unknown'",
        "UPDATE reconciliation_runs SET status = 'completed'",
        "UPDATE reconciliation_runs SET requested_to = requested_from",
    ],
)
async def test_database_refuses_invalid_report_states_and_counts(sessions, statement):
    run_id = uuid.uuid4()
    await reports.begin_run(sessions, run_id, START, END, START, ["steps"])
    with pytest.raises(IntegrityError):
        async with sessions.begin() as session:
            await session.execute(text(statement))
    async with sessions() as session:
        run = await session.get(ReconciliationRun, run_id)
        row = await session.get(ReconciliationResult, (run_id, "steps"))
        assert run.status == "running" and run.finished_at is None
        assert (
            row.status == "pending"
            and row.inserted == row.source_deleted == row.pages_committed == 0
        )
