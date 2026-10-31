"""Committed differences, failure boundaries and restart behavior with real SQLite writers."""

import asyncio
import copy
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from reconciliation_records import END, START, Client, Frozen, execute, point, report, service
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.google_health import pagination
from app.google_health.capture import CapturedResponse
from app.google_health.client import InvalidPageTokenError, Page
from app.google_health.data_types import REGISTRY
from app.models.raw_versions import RawApiResponse, RawHealthDataVersion
from app.models.retrieval import RetrievalCheckpoint, RetrievalEvent, RetrievalReceipt
from app.models.state import RawHealthData, ReliabilityState, SyncState
from app.services import reconciliation_reports as reports
from app.services.retrieval import ReceiptWriter
from app.services.storage import StoreStats, store_records
from app.sync import incremental, reconciliation
from app.sync.ownership import run_ownership


@pytest.fixture(autouse=True)
def fixed_request(monkeypatch):
    monkeypatch.setattr(reconciliation, "datetime", Frozen)


def counts(row):
    return tuple(row[name] for name in ("inserted", "updated", "unchanged", "source_deleted"))


async def archive(sessions, *records):
    async with sessions.begin() as session:
        await store_records(session, REGISTRY["heart-rate"], records)


async def test_new_changed_unchanged_deleted_and_refetch_preserve_raw_history(sessions):
    a, b, missing, new = [point(at=f"2026-09-01T{hour}:00:00Z") for hour in (10, 11, 12, 13)]
    await archive(sessions, a, b, missing)
    changed = copy.deepcopy(b)
    changed["heartRate"]["beatsPerMinute"] = 75
    row, result, _ = await execute(sessions, Page([a, changed], "next"), Page([new], None))

    assert row["status"] == "completed" and row["finished_at"] is not None
    assert (row["requested_from"], row["requested_to"]) == (
        START.replace(tzinfo=None),
        END.replace(tzinfo=None),
    )
    assert counts(row["types"][0]) == (1, 1, 1, 1)
    assert counts(vars(result["heart-rate"])) == (1, 1, 1, 1)
    assert row["types"][0]["pages_committed"] == 2
    repeated, _, _ = await execute(sessions, Page([a, changed, new], None))
    assert counts(repeated["types"][0]) == (0, 0, 3, 0)
    async with sessions() as session:
        raw = list((await session.scalars(select(RawHealthData))).all())
        versions = list((await session.scalars(select(RawHealthDataVersion))).all())
        assert len(raw) == 4 and len(versions) == 1
        assert versions[0].raw_json == b
        deleted = [r for r in raw if r.source_deleted_at is not None]
        assert len(deleted) == 1 and deleted[0].raw_json == missing
        assert await session.scalar(select(func.count()).select_from(RetrievalReceipt)) == 2


async def test_failed_second_page_keeps_first_committed_counts_without_deletions(sessions):
    missing, new = point(at="2026-09-01T10:00:00Z"), point()
    await archive(sessions, missing)
    row, result, runner = await execute(
        sessions, Page([new], "next"), RuntimeError("synthetic private upstream details")
    )
    assert isinstance(result["heart-rate"], RuntimeError)
    assert row["status"] == row["types"][0]["status"] == "partial"
    assert row["error_type"] == "RuntimeError"
    assert counts(row["types"][0]) == (1, 0, 0, 0)
    assert runner.incremental.last_runs["heart-rate"]["inserted"] == 1
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 2
        assert (
            await session.scalar(
                select(func.count()).where(RawHealthData.source_deleted_at.is_not(None))
            )
            == 0
        )
        state = await session.get(SyncState, "heart-rate")
        assert state.last_page_token == "next"
    # Same requested range: only the newly committed continuation belongs to this run.
    resumed, _, runner = await execute(sessions, Page([point()], None))
    assert runner.incremental.client.tokens == ["next"]
    assert resumed["status"] == "completed"
    assert counts(resumed["types"][0]) == (1, 0, 0, 0)


@pytest.mark.parametrize("fail_page", [1, 2])
async def test_page_and_report_counter_rollback_together(sessions, monkeypatch, fail_page):
    original = incremental.record_changes
    calls = 0

    async def fail_after_counter(session, data_type, stats, **kwargs):
        nonlocal calls
        await original(session, data_type, stats, **kwargs)
        calls += 1
        if calls == fail_page:
            raise RuntimeError("synthetic failure after report and checkpoint writes")

    monkeypatch.setattr(incremental, "record_changes", fail_after_counter)
    row, result, runner = await execute(sessions, Page([point()], "next"), Page([point()], None))
    assert isinstance(result["heart-rate"], RuntimeError)
    assert counts(row["types"][0]) == (fail_page - 1, 0, 0, 0)
    assert row["types"][0]["pages_committed"] == fail_page - 1
    assert row["status"] == ("failed" if fail_page == 1 else "partial")
    assert runner.incremental.last_runs["heart-rate"]["inserted"] == fail_page - 1
    async with sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(RawHealthData)) == fail_page - 1
        )
        checkpoint = await session.get(RetrievalCheckpoint, ("sync", "heart-rate"))
        assert (checkpoint is not None) == (fail_page == 2)
        events = list(
            (await session.scalars(select(RetrievalEvent).order_by(RetrievalEvent.id))).all()
        )
        assert [e.kind for e in events] == (["page", "failed"] if fail_page == 2 else ["failed"])


@pytest.mark.parametrize("reset", [False, True])
async def test_deletion_and_report_counter_rollback_together(sessions, monkeypatch, reset):
    missing = point()
    await archive(sessions, missing)
    original = ReceiptWriter.event

    def fail_completion(self, session, kind, **kwargs):
        if kind == "completed":
            raise RuntimeError("synthetic failure after deletion report write")
        return original(self, session, kind, **kwargs)

    monkeypatch.setattr(ReceiptWriter, "event", fail_completion)
    pages = [Page([missing], "expired"), InvalidPageTokenError("expired")] if reset else []
    row, result, runner = await execute(sessions, *pages, Page([], None))
    assert isinstance(result["heart-rate"], RuntimeError)
    assert row["status"] == "partial" and counts(row["types"][0]) == (0, 0, int(reset), 0)
    assert runner.incremental.last_runs["heart-rate"]["failed"] is True
    async with sessions() as session:
        raw = await session.scalar(select(RawHealthData))
        assert raw.source_deleted_at is None and raw.raw_json == missing
        kinds = [e.kind for e in (await session.scalars(select(RetrievalEvent))).all()]
        assert kinds == (["page", "reset", "page", "failed"] if reset else ["page", "failed"])


async def test_many_windows_and_pages_aggregate_commits_and_keep_earlier_windows(sessions):
    specs = {"heart-rate": replace(REGISTRY["heart-rate"], max_window_days=1)}
    a = point(at="2026-08-30T12:00:00Z")
    b = point(at="2026-08-31T12:00:00Z")
    row, results, _ = await execute(
        sessions,
        Page([a], "next"),
        Page([a], None),
        Page([b], None),
        Page([point()], "failed-next"),
        ValueError("synthetic last window failure"),
        days=3,
        specs=specs,
    )
    assert isinstance(results["heart-rate"], ValueError)
    assert row["status"] == "partial"
    assert counts(row["types"][0]) == (3, 0, 1, 0)
    assert row["types"][0]["pages_committed"] == 4
    # The old continuation is completed first, but never counted as the new first window.
    resumed, result, runner = await execute(
        sessions,
        Page([point(at="2026-09-01T13:00:00Z")], None),
        Page([a], None),
        Page([b], None),
        Page([], None),
        days=3,
        specs=specs,
    )
    assert runner.incremental.client.tokens == ["failed-next", None, None, None]
    assert resumed["status"] == "completed"
    assert counts(resumed["types"][0]) == (0, 0, 2, 2)
    assert (
        result["heart-rate"].inserted == 1
    )  # Existing incremental return contract includes resume.


async def test_unrelated_checkpoint_is_not_the_requested_ranges_result(sessions):
    async with sessions.begin() as session:
        session.add(
            SyncState(
                data_type="heart-rate",
                last_page_token="old",
                last_requested_from=START - timedelta(days=10),
                last_requested_to=START - timedelta(days=9),
            )
        )
    row, results, runner = await execute(
        sessions, Page([point(at="2026-08-22T12:00:00Z")], None), Page([point()], None)
    )
    assert runner.incremental.client.tokens == ["old", None]
    assert results["heart-rate"].inserted == 2
    assert counts(row["types"][0]) == (1, 0, 0, 0)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 2


async def test_failure_while_finishing_foreign_checkpoint_counts_nothing(sessions):
    async with sessions.begin() as session:
        session.add(
            SyncState(
                data_type="heart-rate",
                last_page_token="old",
                last_requested_from=START - timedelta(days=10),
                last_requested_to=START - timedelta(days=9),
            )
        )
    row, _, _ = await execute(
        sessions, Page([point(at="2026-08-22T12:00:00Z")], "next-old"), RuntimeError("synthetic")
    )
    assert row["status"] == "failed"
    assert counts(row["types"][0]) == (0, 0, 0, 0)
    assert row["types"][0]["pages_committed"] == 0
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 1


@pytest.mark.parametrize("returns", [False, True])
async def test_token_reset_keeps_committed_changes_and_response_archive(sessions, returns):
    existing, new = point(at="2026-09-01T10:00:00Z"), point()
    await archive(sessions, existing)
    changed = copy.deepcopy(existing)
    changed["heartRate"]["beatsPerMinute"] = 77
    pages = [Page([changed, new], "expired"), Page([changed, new] if returns else [], None)]
    for index, page in enumerate(pages):
        response = CapturedResponse(
            fetched_at=datetime.now(UTC),
            source="reconciliation",
            context_id="synthetic",
            data_type="heart-rate",
            method="GET",
            path="/synthetic",
            query=None,
            request_body=None,
            attempt=1,
            status_code=200,
            headers={},
            body=json.dumps(page.data_points).encode(),
        )
        pages[index] = replace(page, responses=(response,))
    row, result, runner = await execute(
        sessions, pages[0], InvalidPageTokenError("expired"), pages[1]
    )
    assert runner.incremental.client.tokens == [None, "expired", None]
    expected = (1, 1, 2, 0) if returns else (1, 1, 0, 2)
    assert counts(row["types"][0]) == counts(vars(result["heart-rate"])) == expected
    assert row["types"][0]["pages_committed"] == 2
    async with sessions() as session:
        versions = (await session.scalars(select(RawHealthDataVersion))).all()
        assert len(versions) == 1 and versions[0].raw_json == existing
        raw = (await session.scalars(select(RawHealthData))).all()
        assert len(raw) == 2 and sum(r.source_deleted_at is not None for r in raw) == expected[3]
        assert {r.google_data_point_id: r.raw_json for r in raw} == {
            record["name"]: record for record in (changed, new)
        }
        responses = (
            await session.scalars(select(RawApiResponse).order_by(RawApiResponse.id))
        ).all()
        assert [r.body for r in responses] == [p.responses[0].body for p in pages]
        assert all(r.response_id == responses[0].id for r in raw)
        assert await session.scalar(select(func.count()).select_from(RetrievalReceipt)) == 2


@pytest.mark.parametrize("returns", [False, True])
async def test_expired_saved_token_counts_only_committed_restart_differences(sessions, returns):
    existing, new = point(at="2026-09-01T10:00:00Z"), point()
    await archive(sessions, existing)
    changed = copy.deepcopy(existing)
    changed["heartRate"]["beatsPerMinute"] = 77
    first, _, _ = await execute(
        sessions, Page([changed, new], "expired"), RuntimeError("interrupted")
    )
    assert counts(first["types"][0]) == (1, 1, 0, 0)
    row, result, runner = await execute(
        sessions, InvalidPageTokenError("expired"), Page([changed, new] if returns else [], None)
    )
    assert runner.incremental.client.tokens == ["expired", None]
    assert row["status"] == "completed"
    expected = (0, 0, 2, 0) if returns else (0, 0, 0, 2)
    assert counts(row["types"][0]) == counts(vars(result["heart-rate"])) == expected
    assert counts((await report(sessions, first["id"]))["types"][0]) == (1, 1, 0, 0)
    async with sessions() as session:
        raw = (await session.scalars(select(RawHealthData))).all()
        assert len(raw) == 2 and sum(r.source_deleted_at is not None for r in raw) == expected[3]


@pytest.mark.parametrize("first_fails", [True, False])
async def test_type_failure_does_not_stop_other_types(sessions, first_fails):
    specs = {kind: REGISTRY[kind] for kind in ("heart-rate", "steps")}
    pages = [RuntimeError("synthetic private upstream details"), Page([], None)]
    row, result, _ = await execute(sessions, *(pages if first_fails else pages[::-1]), specs=specs)
    assert row["status"] == "partial"
    assert sorted(r["status"] for r in row["types"]) == ["completed", "failed"]
    assert sum(isinstance(value, Exception) for value in result.values()) == 1
    assert row["error_type"] == "RuntimeError"
    assert "synthetic private" not in str(row)


async def test_all_types_failure_is_failed(sessions):
    specs = {kind: REGISTRY[kind] for kind in ("heart-rate", "steps")}
    row, _, _ = await execute(
        sessions, RuntimeError("synthetic"), ValueError("synthetic"), specs=specs
    )
    assert row["status"] == "failed"
    assert all(r["status"] == "failed" for r in row["types"])


@pytest.mark.parametrize("interrupt_outcome", [False, True])
async def test_cancellation_and_process_loss_remain_durable_after_restart(
    tmp_path, monkeypatch, interrupt_outcome
):
    url = f"sqlite+aiosqlite:///{tmp_path / 'reconciliation.sqlite'}"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    waiting = asyncio.Event()

    class Waiting(Client):
        async def list_data_points(self, spec, start, end, page_token=None):
            if page_token:
                waiting.set()
                await asyncio.Event().wait()
            return Page([point()], "second")

    if interrupt_outcome:

        async def lost_outcome(*args):
            raise RuntimeError("simulate process lost before outcome could be persisted")

        monkeypatch.setattr(reports, "finish_run", lost_outcome)
    runner, run_id = (
        service(sessions, Waiting(), {k: REGISTRY[k] for k in ("heart-rate", "steps")}),
        uuid.uuid4(),
    )
    task = asyncio.create_task(runner.run(1, run_id=run_id))
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        live = await report(sessions, run_id)
        assert live["status"] == "running" and live["finished_at"] is None
        assert counts(live["types"][0]) == (1, 0, 0, 0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await engine.dispose()
    # New engine, session factory and reconciliation service; no in-memory result survives.
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        restored = service(sessions)
        assert not restored.running
        persisted = await report(sessions, run_id)
        assert persisted["status"] == ("running" if interrupt_outcome else "cancelled")
        assert counts(persisted["types"][0]) == (1, 0, 0, 0)
        assert persisted["types"][1]["status"] == ("pending" if interrupt_outcome else "cancelled")
        assert await reports.recovery_errors(sessions, run_id)
    finally:
        await engine.dispose()


async def test_counter_scope_is_reset_after_exception(sessions):
    await execute(sessions, RuntimeError("synthetic"))
    # An ordinary sync after reconciliation must not mutate any report.
    runner = service(sessions, Client(Page([point()], None)))
    async with sessions() as session:
        before = await reports.list_runs(session)
    await runner.incremental.run("heart-rate", START, END)
    async with sessions() as session:
        assert await reports.list_runs(session) == before


async def test_report_increment_itself_cannot_commit_when_enclosing_transaction_fails(sessions):
    run_id = uuid.uuid4()
    await reports.begin_run(sessions, run_id, START, END, START, ["heart-rate"])
    await reports.start_type(sessions, run_id, "heart-rate")
    with reports.reporting(run_id, "heart-rate"), pytest.raises(RuntimeError):
        async with sessions.begin() as session:
            await reports.record_changes(session, "heart-rate", StoreStats(inserted=2), page=True)
            raise RuntimeError("synthetic outer rollback")
    row = await report(sessions, run_id)
    assert counts(row["types"][0]) == (0, 0, 0, 0)
    assert row["types"][0]["pages_committed"] == 0


@pytest.mark.parametrize("foreign", [False, True])
async def test_budget_pause_advances_types_without_retry_or_watchdog_success(
    sessions, monkeypatch, foreign
):
    from test_watchdog import _watchdog

    monkeypatch.setattr(pagination, "MAX_PAGES_PER_CALL", 2)
    if foreign:
        async with sessions.begin() as session:
            session.add(
                SyncState(
                    data_type="heart-rate",
                    last_page_token="old",
                    last_requested_from=START - timedelta(days=10),
                    last_requested_to=START - timedelta(days=9),
                )
            )
    specs = {
        "heart-rate": replace(REGISTRY["heart-rate"], max_window_days=1),
        "steps": REGISTRY["steps"],
    }
    client = Client(Page([point()], "p1"), Page([], "p2"), Page([], None))
    runner = service(sessions, client, specs)
    watchdog, _ = _watchdog(sessions, END, runner)
    outcome = await watchdog._recover("watchdog", timedelta(days=3), END)
    assert outcome.action == "failed" and outcome.error_types == ("PaginationPaused",)
    assert client.tokens == ["old" if foreign else None, "p1", None]
    async with sessions() as session:
        history = (await reports.list_runs(session))["runs"][0]
        reliability = await session.get(ReliabilityState, 1)
        state = await session.get(SyncState, "heart-rate")
        assert state.status == "pending" and state.last_page_token == "p2"
        assert state.last_successful_sync is None
        assert reliability.last_recovery_success_at is None
        assert reliability.last_reconciliation_success_at is None
    assert history["status"] == "partial"
    first, second = history["types"]
    assert first["status"] == "partial" and first["pages_committed"] == (0 if foreign else 2)
    assert second["status"] == "completed"
    for kind in specs:
        async with run_ownership(sessions, kind, wait=False) as acquired:
            assert acquired is not None
