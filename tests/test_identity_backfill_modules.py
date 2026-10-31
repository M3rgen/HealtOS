"""Compatibility and transaction boundaries of the remaining backfill split."""

import importlib
import json
import subprocess
import sys
from datetime import timedelta

import pytest
from identity_records import HEART, T0, heart, legacy
from sqlalchemy import event, func, select

from app.db.base import Base
from app.models.identity import (
    IdentityReconcileState,
    RawHealthDataIdentity,
    RawHealthDataSupersession,
    RawSourceAgreement,
)
from app.models.raw_versions import RawHealthDataEvent
from app.models.state import RawHealthData
from app.services import identity_backfill as backfill
from app.services import identity_backfill_agreement as agreement
from app.services import identity_backfill_linking as linking
from app.services import identity_backfill_planning as planning
from app.services import identity_backfill_reading as reading
from app.services.identity_backfill_stats import BackfillStats
from app.services.source_chains import Chains, LockBusy


@pytest.mark.parametrize(
    ("module", "names"),
    [
        ("planning", "_Plan _Link _links_of _plan_batch"),
        ("linking", "_write_plan _batch_chains"),
        (
            "agreement",
            "_review_agreeing _tally_agreement _record_agreement _agreement_of "
            "_count_shared summed_agreement _summed _mark_counted",
        ),
        (
            "state",
            "DeviceChange _prepare _reconcile_row _owe_settlement _settled _chunked "
            "_deferred_of _reconcile_state _save_state",
        ),
    ],
)
def test_old_imports_export_the_extracted_implementations(module, names):
    implementation = importlib.import_module(f"app.services.identity_backfill_{module}")
    for name in names.split():
        exported = getattr(backfill, name)
        assert exported is getattr(implementation, name)
        assert exported.__module__ == implementation.__name__


@pytest.mark.parametrize("module", ["state", "chains", "planning", "agreement", "linking"])
def test_modules_import_before_the_executor_and_production_callers(module):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import app.services.identity_backfill_{module}; import sys; "
            "assert 'app.services.identity_backfill' not in sys.modules; "
            "import app.services.identity_backfill; import app.services.identity_decisions; "
            "import app.services.identity_reconciler; import app.services.identity_verification; "
            "import app.sync.deletions; import app.cli.main",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


async def snapshot(sessions):
    async with sessions() as session:
        return {
            table.name: sorted(
                json.dumps(dict(row), sort_keys=True, default=str)
                for row in (await session.execute(select(table))).mappings()
            )
            for table in Base.metadata.sorted_tables
        }


@pytest.mark.parametrize("incremental", [False, True])
async def test_extracted_dry_run_takes_no_lock_and_changes_no_archive_table(
    sessions, clock, monkeypatch, incremental
):
    await legacy(sessions, HEART, heart(60), T0)
    if incremental:
        result = await backfill.identity_backfill(sessions, data_types=[HEART.name])
        assert result[HEART.name].error is None
    clock.advance(minutes=1)
    raw_id = await legacy(sessions, HEART, heart(60, civil=True), clock.now)
    async with sessions.begin() as session:
        row = await session.get(RawHealthData, raw_id)
        row.payload_received_at = row.updated_at = clock.now
    before = await snapshot(sessions)

    async def forbidden_lock(*args, **kwargs):
        pytest.fail("a dry run attempted to acquire the data type lock")

    monkeypatch.setattr(backfill, "lock_data_type", forbidden_lock)
    engine = sessions.kw["bind"].sync_engine
    statements = []

    def read_only(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
        assert statement.lstrip().split()[0].upper() not in {"INSERT", "UPDATE", "DELETE"}
        assert "FOR UPDATE" not in statement.upper()

    event.listen(engine, "before_cursor_execute", read_only)
    try:
        result = await backfill.identity_backfill(
            sessions, data_types=[HEART.name], dry_run=True, incremental=incremental, batch_size=1
        )
    finally:
        event.remove(engine, "before_cursor_execute", read_only)
    stats = result[HEART.name]
    assert stats.error is None
    assert stats.mode == ("incremental" if incremental else "full")
    assert sum(stats.linked.values()) == 1
    assert statements and await snapshot(sessions) == before


async def test_planning_and_writing_share_the_callers_rollback_and_are_idempotent(sessions):
    await legacy(sessions, HEART, heart(60), T0)
    await legacy(sessions, HEART, heart(60, civil=True), T0 + timedelta(hours=1))
    before = await snapshot(sessions)
    async with sessions() as session:
        batch = await reading._read_batch(session, HEART.name, 1, None, None)
        rows, stale = reading._rows_of(HEART, batch, None)
        assert len(rows) == 2 and not stale
        stats = BackfillStats()
        plan = await planning._plan_batch(session, HEART, rows, Chains(), stats)
        assert len(plan.links) == 1 and len(plan.aliases) == 1
        await linking._write_plan(session, HEART, plan, stats, dry_run=False)
        assert sum(stats.linked.values()) == stats.aliases_added == 1
        assert await session.scalar(select(func.count()).select_from(RawHealthDataEvent)) == 1
        await session.rollback()
    assert await snapshot(sessions) == before

    for expected in (1, 0):
        async with sessions.begin() as session:
            stats = BackfillStats()
            await linking._write_plan(session, HEART, plan, stats, dry_run=False)
            assert sum(stats.linked.values()) == stats.aliases_added == expected
    async with sessions() as session:
        for table in (RawHealthDataIdentity, RawHealthDataSupersession, RawHealthDataEvent):
            assert await session.scalar(select(func.count()).select_from(table)) == 1
    after = await snapshot(sessions)
    assert after[RawHealthData.__tablename__] == before[RawHealthData.__tablename__]


async def test_agreement_boundary_appends_changes_and_recounts_latest_keys(sessions):
    pair = ("first", "second")
    first = {(pair, "key-1"): True, (pair, "key-2"): False}
    async with sessions.begin() as session:
        await agreement._record_agreement(session, HEART.name, first)
        await agreement._record_agreement(session, HEART.name, first)
        assert await session.scalar(select(func.count()).select_from(RawSourceAgreement)) == 2
        await agreement._record_agreement(session, HEART.name, {(pair, "key-2"): True})
        assert await session.scalar(select(func.count()).select_from(RawSourceAgreement)) == 3
        assert await agreement.summed_agreement(session) == {HEART.name: {pair: (2, 0)}}
    assert await agreement._mark_counted(sessions, HEART.name, {pair}, recount=True) == {
        pair: (2, 0, True)
    }
    async with sessions() as session:
        entries = (
            await session.scalars(select(RawSourceAgreement).order_by(RawSourceAgreement.id))
        ).all()
        assert [entry.same_reading for entry in entries] == [True, False, True]
        assert await agreement._summed(session, "absent-data-type") == {}


async def test_chain_settlement_uses_legacy_retry_hooks_and_rolls_back_failed_attempts(
    sessions, monkeypatch
):
    attempts, pauses, notices = [], [], []

    async def lock(session, data_type, *, exclusive, wait):
        assert exclusive and wait == backfill.LOCK_TIMEOUT
        attempts.append(data_type)
        assert await session.get(IdentityReconcileState, "failed-attempt") is None
        if len(attempts) < 3:
            session.add(IdentityReconcileState(data_type="failed-attempt", runs=0))
            await session.flush()
            raise LockBusy("busy")

    async def pause(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(backfill, "lock_data_type", lock)
    monkeypatch.setattr(backfill, "_pause", pause)
    stats = BackfillStats()
    planned, change = await backfill._settle_chains(
        sessions, HEART, None, stats, dry_run=False, statement_timeout="120s", notice=notices.append
    )
    assert isinstance(planned, Chains) and not change
    assert attempts == [HEART.name] * 3 and pauses == [2, 4] and len(notices) == 2
    async with sessions() as session:
        assert await session.get(IdentityReconcileState, "failed-attempt") is None
        assert await session.get(IdentityReconcileState, HEART.name) is not None


async def test_exhausted_chain_retries_leave_no_checkpoint_or_partial_writes(sessions, monkeypatch):
    before = await snapshot(sessions)
    pauses = []

    async def busy(*args, **kwargs):
        raise LockBusy("busy")

    async def pause(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(backfill, "lock_data_type", busy)
    monkeypatch.setattr(backfill, "_pause", pause)
    monkeypatch.setattr(backfill, "LOCK_ATTEMPTS", 7)
    with pytest.raises(backfill.LockUnavailable, match="7 attempts"):
        await backfill._settle_chains(
            sessions, HEART, None, BackfillStats(), dry_run=False, statement_timeout="120s"
        )
    assert pauses == [2, 4, 8, 16, 32, 60, 60]
    assert await snapshot(sessions) == before
