"""Receipt migration and production writers on PostgreSQL; synthetic data only."""

import pytest
from derived_records import store
from retrieval_records import START, Client, Runner, point
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.google_health.client import Page
from app.google_health.data_types import REGISTRY
from app.models.retrieval import RECEIPT_TABLES, RetrievalEvent
from app.parsers.common import payload_hash
from app.services.raw_history import verify_guards
from app.services.retrieval_coverage import coverage_report

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("lane", ["sync", "backfill"])
async def test_production_receipt_resume_is_durable_on_postgresql(pg_sessions, lane):
    runner = Runner(pg_sessions, lane)
    with pytest.raises(RuntimeError):
        await runner.run(Client(Page([point()], "next"), RuntimeError("synthetic interruption")))
    await runner.run(Client(Page([], None)))
    async with pg_sessions() as session:
        events = list(
            (await session.scalars(select(RetrievalEvent).order_by(RetrievalEvent.id))).all()
        )
        assert [e.kind for e in events] == ["page", "failed", "page", "completed"]
        assert sum(e.returned_count for e in events) == 1
        report = await coverage_report(
            session, REGISTRY, runner.start.date(), runner.start.date(), "UTC", "heart-rate"
        )
        assert report["types"][0]["days"][0]["retrieval"] == "completed_with_records"
        assert await verify_guards(session) == []


async def test_presence_reads_only_the_source_member_with_json_types(pg_sessions):
    # A boolean source must keep its own fingerprint, distinct from a numeric one.
    sources = [True, 1, {"device": {"model": "Saat"}}, None]
    await store(pg_sessions, "heart-rate", *({**point(), "dataSource": s} for s in sources))
    async with pg_sessions() as session:
        report = await coverage_report(
            session, REGISTRY, START.date(), START.date(), "UTC", "heart-rate"
        )
    regimes = {row["source"]: row["device"] for row in report["types"][0]["source_regimes"]}
    assert regimes == {
        f"kaynak-{payload_hash(source)[:8]}": "Saat" if isinstance(source, dict) else None
        for source in sources
    }


async def test_all_receipt_guards_refuse_changes_even_in_replica_mode(pg_sessions):
    await Runner(pg_sessions, "sync").run(Client())
    for table in RECEIPT_TABLES:
        for operation in (
            f"UPDATE {table} SET id = id",
            f"DELETE FROM {table}",
            f"TRUNCATE {table}",
        ):
            for replica in (False, True):
                with pytest.raises(DBAPIError, match="keeps every record"):
                    async with pg_sessions.begin() as session:
                        if replica:
                            await session.execute(
                                text("SET LOCAL session_replication_role = replica")
                            )
                        await session.execute(text(operation))


async def test_archive_verification_detects_redefined_or_disabled_receipt_guards(pg_sessions):
    async with pg_sessions() as session:
        assert await verify_guards(session) == []
    async with pg_sessions.begin() as session:
        await session.execute(
            text("ALTER TABLE retrieval_events DISABLE TRIGGER retrieval_events_no_truncate")
        )
    async with pg_sessions() as session:
        errors = await verify_guards(session)
    assert any("retrieval_events_no_truncate is disabled" in error for error in errors)
