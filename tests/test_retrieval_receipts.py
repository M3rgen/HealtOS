"""Real sync/backfill writers with synthetic clients and transactional SQLite."""

import asyncio
import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from retrieval_records import BAD_TIMES, END, START, Client, Runner, point
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings, get_settings
from app.google_health.client import InvalidPageTokenError, Page
from app.google_health.data_types import REGISTRY
from app.models import retrieval
from app.models.retrieval import RetrievalCheckpoint
from app.models.retrieval import RetrievalEvent as Event
from app.models.retrieval import RetrievalReceipt as Receipt
from app.models.state import BackfillJob, FetchRound, RawHealthData, SyncState
from app.services import retrieval as retrieval_writer
from app.services import storage
from app.services.raw_history import verify_guards
from app.services.retrieval import query_scope
from app.services.retrieval_coverage import coverage_report
from app.sync import backfill, incremental


async def rows(sessions):
    async with sessions() as session:
        return (
            list((await session.scalars(select(Receipt).order_by(Receipt.started_at))).all()),
            list((await session.scalars(select(Event).order_by(Event.id))).all()),
        )


async def status(sessions, runner):
    async with sessions() as session:
        report = await coverage_report(
            session, REGISTRY, runner.start.date(), runner.start.date(), "UTC", runner.kind
        )
    return report["types"][0]["days"][0]


@pytest.fixture(params=["sync", "backfill"])
def runner(sessions, request):
    return Runner(sessions, request.param)


@pytest.mark.parametrize("kind,at", [("heart-rate", "2026-09-01T12:00:00Z"), *BAD_TIMES])
async def test_empty_and_recorded_success_count_returns_not_upserts(sessions, runner, kind, at):
    runner.kind = kind
    invalid = (kind, at) in BAD_TIMES
    record = point(at, kind)
    await runner.run(Client(Page([record], "p2"), Page([record], None)))
    await runner.run(Client(Page([record], None)))
    receipts, events = await rows(sessions)
    runs, pages = (2, 3) if runner.lane == "sync" else (1, 2)
    assert len(receipts) == len([e for e in events if e.kind == "completed"]) == runs
    assert [e.returned_count for e in events if e.kind == "page"] == [1] * pages
    assert [e.unplaced_count for e in events if e.kind == "page"] == [int(invalid)] * pages
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 1
    expected = "unknown" if invalid else "completed_with_records"
    assert (await status(sessions, runner))["retrieval"] == expected
    # A separate day, not the previous nonempty day with a newer empty snapshot.
    empty = Runner(sessions, runner.lane, kind, start=END, end=datetime(2026, 9, 3, tzinfo=UTC))
    await empty.run(Client())
    assert (await status(sessions, empty))["retrieval"] == "completed_empty"


async def test_second_page_failure_and_durable_resume(sessions, runner):
    first = Client(Page([point()], "next-page"), RuntimeError("synthetic second page failure"))
    with pytest.raises(RuntimeError):
        await runner.run(first)
    before, events = await rows(sessions)
    assert [e.kind for e in events] == ["page", "failed"]
    assert (await status(sessions, runner))["retrieval"] == "incomplete"
    resumed = Client(Page([], None))
    await runner.run(resumed)
    after, events = await rows(sessions)
    assert resumed.tokens == ["next-page"]
    assert after[0].id == before[0].id
    assert after[1].starts_first is False
    assert after[1].parent_event_id == events[0].id
    assert after[1].resume_token_hash != "next-page"
    assert (await status(sessions, runner))["retrieval"] == "completed_with_records"


async def test_legacy_checkpoint_does_not_prove_first_page(sessions, runner):
    # Mutable status can describe an earlier successful fetch but is not evidence.
    if runner.lane == "sync":
        async with sessions.begin() as session:
            session.add(
                SyncState(
                    data_type=runner.kind,
                    last_page_token="legacy",
                    last_requested_from=START,
                    last_requested_to=END,
                )
            )
    else:
        runner.job_id = uuid.uuid4()
        async with sessions.begin() as session:
            session.add(
                BackfillJob(
                    id=runner.job_id,
                    data_type=runner.kind,
                    page_token="legacy",
                    window_start=START,
                    window_end=END,
                )
            )
    await runner.run(Client(Page([], None)))
    receipts, _ = await rows(sessions)
    assert receipts[0].parent_event_id is None
    assert receipts[0].starts_first is False
    assert (await status(sessions, runner))["retrieval"] == "incomplete"


async def test_token_reset_preserves_aborted_attempt_and_starts_at_first_page(sessions, runner):
    with pytest.raises(RuntimeError):
        await runner.run(Client(Page([point()], "expired"), RuntimeError("interruption")))
    client = Client(InvalidPageTokenError("expired"), Page([], "fresh"), Page([], None))
    await runner.run(client)
    receipts, events = await rows(sessions)
    assert client.tokens == ["expired", None, "fresh"]
    assert len(receipts) == 3
    assert [r.starts_first for r in receipts] == [True, False, True]
    assert [e.kind for e in events] == ["page", "failed", "reset", "page", "page", "completed"]
    assert events[-1].receipt_id == receipts[-1].id
    # The old attempt's returned point must not contaminate the new empty snapshot.
    assert (await status(sessions, runner))["retrieval"] == "completed_empty"


async def test_rollback_keeps_attempt_but_no_durable_page_or_completion(
    sessions, runner, monkeypatch
):
    module = incremental if runner.lane == "sync" else backfill
    original = module.store_page

    async def rollback(session, *args, **kwargs):
        await original(session, *args, **kwargs)
        raise RuntimeError("synthetic transaction rollback")

    monkeypatch.setattr(module, "store_page", rollback)
    client = Client(Page([point()], None))
    with pytest.raises(RuntimeError):
        await runner.run(client)
    receipts, events = await rows(sessions)
    assert len(receipts) == 1 and [e.kind for e in events] == ["failed"]
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 0
    assert len(client.kept) == 1
    assert events[0].returned_count == 1  # received, although the page was rolled back
    assert (await status(sessions, runner))["retrieval"] == "incomplete"


async def test_inflight_interruption_and_cancellation_never_claim_completion(sessions, runner):
    waiting = asyncio.Event()

    class Interrupted(Client):
        async def list_data_points(self, spec, start, end, page_token=None):
            if not page_token:
                return Page([point()], "second")
            waiting.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(runner.run(Interrupted()))
    await asyncio.wait_for(waiting.wait(), 5)
    try:
        # As if the process stopped here: first page committed, no outcome ever written.
        _, events = await rows(sessions)
        assert [e.kind for e in events] == ["page"]
        assert (await status(sessions, runner))["retrieval"] == "incomplete"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    _, events = await rows(sessions)
    assert [e.kind for e in events] == ["page", "cancelled"]
    assert (await status(sessions, runner))["retrieval"] == "incomplete"


async def test_last_page_is_not_completion_when_final_transaction_rolls_back(sessions, monkeypatch):
    async def failed_final(*args, **kwargs):
        raise RuntimeError("final transaction failed")

    monkeypatch.setattr(incremental, "mark_source_deletions", failed_final)
    runner = Runner(sessions, "sync")
    with pytest.raises(RuntimeError):
        await runner.run(Client(Page([point()], None)))
    _, events = await rows(sessions)
    assert events[0].last_page
    assert [e.kind for e in events] == ["page", "failed"]
    assert (await status(sessions, runner))["retrieval"] == "incomplete"


@pytest.mark.parametrize("table", retrieval.RECEIPT_TABLES)
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
async def test_sqlite_evidence_is_append_only(sessions, table, operation):
    await Runner(sessions, "sync").run(Client())
    statement = f"DELETE FROM {table}" if operation == "DELETE" else (f"UPDATE {table} SET id = id")
    async with sessions.begin() as session:
        with pytest.raises(DBAPIError, match="keeps every record"):
            await session.execute(text(statement))
    async with sessions() as session:
        assert await verify_guards(session) == []


def test_scope_matches_daily_seconds_rollup_sleep_civil_and_unbounded():
    start = datetime(2026, 9, 1, 0, 0, 30, 999, UTC)
    end = datetime(2026, 9, 1, 1, 0, 30, 999, UTC)
    assert query_scope(REGISTRY["heart-rate"], start, end)[:2] == (
        start.replace(microsecond=0),
        end.replace(second=31, microsecond=0),
    )
    assert query_scope(REGISTRY["daily-resting-heart-rate"], start, end)[:2] == (START, END)
    lower, upper, _, basis = query_scope(REGISTRY["total-calories"], start, end)
    assert lower == START and upper == end.replace(minute=1, second=0, microsecond=0)
    assert basis == "rollup_start"
    assert query_scope(REGISTRY["sleep"], start, end)[3] == "physical_end"
    assert query_scope(REGISTRY["exercise"], start, end)[3] == "civil_start"
    assert query_scope(REGISTRY["food"], start, end) == (None, None, None, "unbounded")
    assert query_scope(REGISTRY["electrocardiogram"], start, end)[1] is None


def test_migration_is_additive_and_guard_sql_frozen(tmp_path, monkeypatch):
    path = Path("migrations/versions/20261025_0015_retrieval_receipts.py")
    spec = importlib.util.spec_from_file_location("receipt_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.POSTGRESQL_DDL == retrieval.POSTGRESQL_DDL
    assert migration.SQLITE_DDL == retrieval.SQLITE_DDL
    assert migration.down_revision == "20261024_0014"
    url = f"sqlite:///{tmp_path / 'receipts.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    engine = create_engine(url)
    try:
        command.upgrade(config, "20261024_0014")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO manual_measurements "
                    "(id, metric, value, measured_at) "
                    "VALUES (:id, 'weight', 70, '2026-09-01')"
                ),
                {"id": uuid.uuid4().hex},
            )
            connection.execute(
                text(
                    "INSERT INTO raw_health_data "
                    "(id, data_type, google_data_point_id, raw_json, payload_hash, "
                    "first_seen_at, last_seen_at) VALUES "
                    "(:id, 'heart-rate', 'synthetic', '{}', 'synthetic', "
                    "'2026-09-01', '2026-09-01')"
                ),
                {"id": uuid.uuid4().hex},
            )
            before = connection.execute(text("SELECT * FROM raw_health_data")).all()
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT * FROM raw_health_data")).all() == before
            assert connection.scalar(text("SELECT count(*) FROM manual_measurements")) == 1
            assert set(retrieval.RECEIPT_TABLES) <= set(inspect(connection).get_table_names())
            for name, (_, sql) in retrieval.SQLITE_TRIGGER_SQL.items():
                installed = connection.scalar(
                    text("SELECT sql FROM sqlite_master WHERE name=:n"), {"n": name}
                )
                assert installed == sql
        command.downgrade(config, "20261024_0014")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT * FROM raw_health_data")).all() == before
        command.upgrade(config, "head")

        async def create_evidence():
            from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

            writer_engine = create_async_engine(url.replace("sqlite:", "sqlite+aiosqlite:"))
            try:
                await Runner(async_sessionmaker(writer_engine, expire_on_commit=False), "sync").run(
                    Client()
                )
            finally:
                await writer_engine.dispose()

        asyncio.run(create_evidence())
        with pytest.raises(RuntimeError, match="holds retrieval evidence; downgrade refused"):
            command.downgrade(config, "20261024_0014")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM retrieval_receipts")) == 1
            assert connection.scalar(text("SELECT count(*) FROM retrieval_events")) == 2
            assert connection.execute(text("SELECT * FROM raw_health_data")).all() == before
    finally:
        engine.dispose()
        get_settings.cache_clear()


@pytest.mark.parametrize("table", ["retrieval_events", "retrieval_checkpoints"])
async def test_page_or_pointer_failure_rolls_back_records_and_checkpoint(sessions, runner, table):
    async with sessions.begin() as session:
        when = " WHEN NEW.kind = 'page'" if table == "retrieval_events" else ""
        await session.execute(
            text(f"""CREATE TRIGGER synthetic_page_failure
            BEFORE INSERT ON {table}{when}
            BEGIN SELECT RAISE(ABORT, 'synthetic page event failure'); END""")
        )
    with pytest.raises(DBAPIError, match="synthetic page event failure"):
        await runner.run(Client(Page([point()], "second")))
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 0
        if runner.lane == "sync":
            state = await session.get(SyncState, runner.kind)
            assert state.last_page_token is None and state.records_downloaded == 0
        else:
            job = await session.get(BackfillJob, runner.job_id)
            assert job.page_token is None and job.records_downloaded == 0
    _, events = await rows(sessions)
    assert [e.kind for e in events] == ["failed"]


async def test_repeated_token_does_not_create_completion(sessions, runner):
    with pytest.raises(RuntimeError, match="repeated"):
        await runner.run(Client(Page([], "repeat"), Page([], "repeat")))
    _, events = await rows(sessions)
    assert [e.kind for e in events] == ["page", "failed"]
    assert (await status(sessions, runner))["retrieval"] == "incomplete"


async def test_verifier_checks_new_sqlite_triggers(sessions):
    async with sessions.begin() as session:
        await session.execute(text("DROP TRIGGER retrieval_events_no_delete"))
    async with sessions() as session:
        assert await verify_guards(session) == [
            "protective trigger missing: retrieval_events.retrieval_events_no_delete"
        ]


async def test_resume_after_repeated_token_can_complete_the_valid_chain(sessions, runner):
    with pytest.raises(RuntimeError, match="repeated"):
        await runner.run(Client(Page([], "repeat"), Page([], "repeat")))
    await runner.run(Client(Page([], None)))
    assert (await status(sessions, runner))["retrieval"] == "completed_empty"


async def test_webhook_processing_uses_the_production_receipt_writer(sessions):
    from app.models.state import OAuthToken
    from app.services.webhook_processing import WebhookBatchProcessor

    async with sessions.begin() as session:
        session.add(
            OAuthToken(
                subject="me",
                encrypted_refresh_token="synthetic",
                health_user_id="synthetic-user",
            )
        )
    sync = incremental.IncrementalSync(
        Settings(), sessions, Client(), {"heart-rate": REGISTRY["heart-rate"]}
    )
    processor = WebhookBatchProcessor(sessions, sync, {"heart-rate"})
    ingested = await processor.ingest_verified(
        b'{"synthetic":true}',
        {
            "data": {
                "dataType": "heart-rate",
                "healthUserId": "synthetic-user",
                "operation": "UPSERT",
                "intervals": [
                    {
                        "physicalTimeInterval": {
                            "startTime": START.isoformat(),
                            "endTime": END.isoformat(),
                        }
                    }
                ],
            }
        },
    )
    result = await processor.process_batch(ingested.batch_hash)
    assert result.completed_elements == 1
    receipts, events = await rows(sessions)
    assert len(receipts) == 1 and receipts[0].lane == "sync"
    assert [e.kind for e in events] == ["page", "completed"]
    assert (await status(sessions, Runner(sessions, "sync")))["retrieval"] == "completed_empty"


async def test_reconciliation_uses_the_production_receipt_writer(sessions):
    from datetime import timedelta

    from app.sync.reconciliation import ReconciliationService

    sync = incremental.IncrementalSync(
        Settings(), sessions, Client(), {"heart-rate": REGISTRY["heart-rate"]}
    )
    await ReconciliationService(sync, sessions).run(3)
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    async with sessions() as session:
        report = await coverage_report(session, REGISTRY, yesterday, yesterday, "UTC", "heart-rate")
    assert report["types"][0]["days"][0]["retrieval"] == "completed_empty"
    receipts, _ = await rows(sessions)
    assert len(receipts) == 1 and receipts[0].lane == "sync"


async def test_resume_does_not_guess_a_parent_from_a_matching_old_token(sessions, runner):
    from sqlalchemy import delete

    from app.models.retrieval import RetrievalCheckpoint

    with pytest.raises(RuntimeError):
        await runner.run(Client(Page([point()], "reused"), RuntimeError("interruption")))
    # A legacy/imported checkpoint can carry the same opaque token as an old receipt.
    # Without the atomic pointer that old receipt is not proof of this continuation.
    async with sessions.begin() as session:
        await session.execute(delete(RetrievalCheckpoint))
    await runner.run(Client(Page([], None)))
    receipts, _ = await rows(sessions)
    assert receipts[-1].parent_event_id is None
    assert (await status(sessions, runner))["retrieval"] == "incomplete"


@pytest.fixture
def attempt_clock(clock, runner, monkeypatch):
    clock.now = datetime(2026, 10, 1, tzinfo=UTC)
    for module in (incremental, backfill, retrieval_writer):
        monkeypatch.setattr(module, "datetime", storage.datetime)
    module = incremental if runner.lane == "sync" else backfill
    original, starts = module.store_page, []

    async def recording(session, spec, page, **options):
        starts.append(options["fetch_started"])
        return await original(session, spec, page, **options)

    monkeypatch.setattr(module, "store_page", recording)
    return clock, starts


async def checkpoint(sessions, runner):
    owner = runner.kind if runner.lane == "sync" else str(runner.job_id)
    async with sessions() as session:
        state = await session.get(
            SyncState if runner.lane == "sync" else BackfillJob,
            runner.kind if runner.lane == "sync" else runner.job_id,
        )
        token = state.last_page_token if runner.lane == "sync" else state.page_token
        boundary = await session.get(FetchRound, (runner.lane, owner))
        pointer = await session.get(RetrievalCheckpoint, (runner.lane, owner))
        return token, boundary.started_at, pointer.event_id if pointer else None


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("returns", [False, True])
async def test_reset_moves_boundary_and_only_new_attempt_keeps_records_live(
    sessions, runner, attempt_clock, resume, returns
):
    clock, starts = attempt_clock
    record = point()
    initial = clock.now
    if resume:
        with pytest.raises(RuntimeError):
            await runner.run(Client(Page([record], "expired"), RuntimeError("interrupted")))
        before, old_events = await rows(sessions)

    class Restarting(Client):
        async def list_data_points(self, spec, start, end, page_token=None):
            if page_token == "expired":
                clock.advance(minutes=1)
            return await super().list_data_points(spec, start, end, page_token)

    pages = [] if resume else [Page([record], "expired")]
    await runner.run(
        Restarting(
            *pages, InvalidPageTokenError("expired"), Page([record] if returns else [], None)
        )
    )
    receipts, events = await rows(sessions)
    assert [stamp.replace(tzinfo=UTC) for stamp in starts] == [initial, clock.now]
    token, boundary, _ = await checkpoint(sessions, runner)
    assert token is None and boundary == clock.now.replace(tzinfo=None)
    assert receipts[-1].started_at == boundary and receipts[-1].starts_first
    assert receipts[-1].parent_event_id is None
    if resume:
        assert all(
            getattr(receipts[0], c.name) == getattr(before[0], c.name)
            for c in Receipt.__table__.columns
        )
        assert all(
            getattr(a, c.name) == getattr(b, c.name)
            for a, b in zip(events[:2], old_events, strict=True)
            for c in Event.__table__.columns
        )
    async with sessions() as session:
        raw = (await session.scalars(select(RawHealthData))).one()
        assert raw.raw_json == record
        assert (raw.source_deleted_at is not None) == (runner.lane == "sync" and not returns)
    expected = "completed_with_records" if returns else "completed_empty"
    assert (await status(sessions, runner))["retrieval"] == expected


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_interruption_after_reset_keeps_checkpoint_and_attempt_for_continuation(
    sessions, runner, attempt_clock, committed, cancel
):
    clock, starts = attempt_clock
    record = point()
    with pytest.raises(RuntimeError):
        await runner.run(Client(Page([record], "expired"), RuntimeError("interrupted")))
    clock.advance(minutes=1)
    reset_at = clock.now
    failure = asyncio.CancelledError() if cancel else RuntimeError("new attempt interrupted")

    class Inspecting(Client):
        async def list_data_points(self, spec, start, end, page_token=None):
            if page_token is None:
                token, boundary, pointer = await checkpoint(sessions, runner)
                receipts, events = await rows(sessions)
                assert token is None and pointer is None
                assert boundary == receipts[-1].started_at == reset_at.replace(tzinfo=None)
                assert events[-1].kind == "reset" and receipts[-1].starts_first
            return await super().list_data_points(spec, start, end, page_token)

    pages = [Page([record], "new-next")] if committed else []
    with pytest.raises(type(failure)):
        await runner.run(Inspecting(InvalidPageTokenError("expired"), *pages, failure))
    token, boundary, pointer = await checkpoint(sessions, runner)
    assert token == ("new-next" if committed else None)
    assert boundary == reset_at.replace(tzinfo=None) and bool(pointer) == committed
    async with sessions() as session:
        assert (await session.scalars(select(RawHealthData))).one().source_deleted_at is None
    assert (await status(sessions, runner))["retrieval"] == "incomplete"
    clock.advance(minutes=1)
    client = Client(Page([record], None))
    await runner.run(client)
    assert client.tokens == (["new-next"] if committed else [None])
    expected_start = reset_at if committed else clock.now
    assert starts[-1] == expected_start
    assert (await checkpoint(sessions, runner))[1] == expected_start.replace(tzinfo=None)
    assert (await status(sessions, runner))["retrieval"] == "completed_with_records"


@pytest.mark.parametrize("target", ["receipt", "boundary", "token", "pointer"])
async def test_reset_transaction_rollback_preserves_old_attempt(sessions, runner, target):
    with pytest.raises(RuntimeError):
        await runner.run(Client(Page([point()], "expired"), RuntimeError("interrupted")))
    before = await checkpoint(sessions, runner)
    receipts, _ = await rows(sessions)
    table, operation = {
        "receipt": ("retrieval_receipts", "INSERT"),
        "boundary": ("fetch_rounds", "UPDATE"),
        "token": ("sync_state" if runner.lane == "sync" else "backfill_jobs", "UPDATE"),
        "pointer": ("retrieval_checkpoints", "DELETE"),
    }[target]
    condition = ""
    if target == "token":
        column = "last_page_token" if runner.lane == "sync" else "page_token"
        condition = f" WHEN OLD.{column} IS NOT NULL AND NEW.{column} IS NULL"

    # Install only when the invalid-token callback runs: ordinary receipt begin succeeds.
    class Rejecting(Client):
        async def list_data_points(self, *args):
            async with sessions.begin() as session:
                await session.execute(
                    text(f"""CREATE TRIGGER reject_reset BEFORE {operation}
                    ON {table}{condition}
                    BEGIN SELECT RAISE(ABORT, 'reset rolled back'); END""")
                )
            raise InvalidPageTokenError("expired")

    with pytest.raises(DBAPIError, match="reset rolled back"):
        await runner.run(Rejecting())
    assert await checkpoint(sessions, runner) == before
    after, events = await rows(sessions)
    assert len(after) == len(receipts) + 1  # failed continuation, no replacement receipt
    assert not any(e.kind in ("reset", "completed") for e in events)
    assert events[-1].kind == "failed" and events[-1].receipt_id == after[-1].id
    async with sessions.begin() as session:
        await session.execute(text("DROP TRIGGER reject_reset"))
    client = Client(Page([], None))
    await runner.run(client)
    assert client.tokens == ["expired"]


@pytest.mark.parametrize("failure", [InvalidPageTokenError("again"), RuntimeError("repeated")])
async def test_reset_still_allows_only_one_restart_and_rejects_repeated_tokens(
    sessions, runner, failure
):
    tail = [failure] if isinstance(failure, InvalidPageTokenError) else [Page([], "x")]
    client = Client(Page([], "expired"), InvalidPageTokenError("expired"), Page([], "x"), *tail)
    with pytest.raises(type(failure)):
        await runner.run(client)
    _, events = await rows(sessions)
    assert sum(e.kind == "reset" for e in events) == 1
    assert not any(e.kind == "completed" for e in events)
    assert (await status(sessions, runner))["retrieval"] == "incomplete"
