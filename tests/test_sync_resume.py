from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from numeric_records import BAD_NUMBERS, numeric_point
from retrieval_records import BAD_TIMES, START, Client, Runner, point
from sqlalchemy import func, select

from app.api.sync import _serialize
from app.core.config import Settings
from app.google_health.client import Page
from app.google_health.data_types import REGISTRY
from app.models.retrieval import RetrievalEvent
from app.models.state import (
    BackfillJob,
    JobStatus,
    OAuthToken,
    RawHealthData,
    SyncState,
    WebhookNotification,
)
from app.services.webhook_processing import WebhookBatchProcessor
from app.sync.backfill import BackfillService
from app.sync.incremental import IncrementalSync


class FakeClient:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.tokens = []

    async def list_data_points(self, spec, start, end, page_token=None):
        self.tokens.append(page_token)
        return next(self.pages)


@pytest.mark.parametrize("kind, bad", BAD_TIMES)
@pytest.mark.parametrize("named", [False, True])
async def test_incremental_sync_commits_checkpoint(sessions, kind, bad, named):
    records = [point(bad, kind, named=named), point(kind=kind, named=named)]
    client = FakeClient([Page(records, "p2"), Page([], None)])
    service = IncrementalSync(Settings(), sessions, client, {kind: REGISTRY[kind]})
    await service.run(kind)
    async with sessions() as session:
        state = await session.get(SyncState, kind)
        assert sorted(await session.scalars(select(RetrievalEvent.unplaced_count))) == [0, 0, 1]
    assert client.tokens == [None, "p2"]
    assert state.status == JobStatus.completed
    assert state.last_page_token is None
    assert state.last_successful_sync is not None
    assert state.records_downloaded == len(records)


async def test_targeted_historical_sync_does_not_regress_checkpoint(sessions):
    current = datetime(2026, 8, 18, tzinfo=UTC)
    async with sessions.begin() as session:
        session.add(
            SyncState(
                data_type="steps",
                status=JobStatus.completed,
                last_successful_sync=current,
            )
        )
    service = IncrementalSync(
        Settings(), sessions, FakeClient([Page([], None)]), {"steps": REGISTRY["steps"]}
    )

    await service.run(
        "steps",
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
    )

    async with sessions() as session:
        state = await session.get(SyncState, "steps")
    assert state.last_successful_sync.replace(tzinfo=UTC) == current


async def test_backfill_resumes_saved_page_token(sessions):
    start = datetime.now(UTC) - timedelta(days=1)
    end = datetime.now(UTC) + timedelta(days=1)
    async with sessions.begin() as session:
        session.add(
            BackfillJob(
                data_type="steps",
                window_start=start,
                window_end=end,
                status=JobStatus.running,
                page_token="resume-me",
            )
        )
    client = FakeClient([Page([], None)])
    settings = Settings(backfill_start_date=end.date(), backfill_window_days=31)
    service = BackfillService(settings, sessions, client, {"steps": REGISTRY["steps"]})
    assert await service.run(["steps"]) == 1
    assert client.tokens == ["resume-me"]
    async with sessions() as session:
        job = await session.scalar(select(BackfillJob))
    assert job.status == JobStatus.completed


@pytest.mark.parametrize("paused", [False, True])
async def test_backfill_failure_does_not_block_other_data_types(sessions, monkeypatch, paused):
    from app.google_health import pagination

    monkeypatch.setattr(pagination, "MAX_PAGES_PER_CALL", 2)
    tokens = []
    terminal = False

    class IsolatedClient:
        async def list_data_points(self, spec, start, end, page_token=None):
            if spec.name == "steps":
                tokens.append(page_token)
                if terminal:
                    return Page([], None)
                if paused:
                    return Page([], f"p{len(tokens)}")
                raise RuntimeError("unavailable scope")
            return Page([], None)

    settings = Settings(backfill_start_date=datetime.now(UTC).date())
    service = BackfillService(
        settings,
        sessions,
        IsolatedClient(),
        {"steps": REGISTRY["steps"], "sleep": REGISTRY["sleep"]},
    )
    assert await service.run() == 1
    async with sessions() as session:
        jobs = (await session.scalars(select(BackfillJob))).all()
    statuses = {job.data_type: job.status for job in jobs}
    assert statuses == {
        "steps": JobStatus.pending if paused else JobStatus.failed,
        "sleep": JobStatus.completed,
    }
    if paused:
        assert tokens == [None, "p1"]
        terminal = True
        assert await service.run(["steps"]) == 1
        assert tokens == [None, "p1", "p2"]


async def test_unfiltered_reference_backfill_creates_only_one_snapshot_job(sessions):
    settings = Settings(backfill_start_date=datetime.now(UTC).date())
    service = BackfillService(
        settings,
        sessions,
        FakeClient([Page([], None)]),
        {"food": REGISTRY["food"]},
    )

    assert await service.run(["food"]) == 1
    assert await service.run(["food"]) == 0
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(BackfillJob)) == 1


def heart_rate(bpm=60):
    return {
        "name": "users/1/dataTypes/heart-rate/dataPoints/abc",
        "heartRate": {
            "sampleTime": {"physicalTime": "2026-08-01T10:00:00Z", "utcOffset": "0s"},
            "beatsPerMinute": bpm,
        },
    }


async def test_incremental_run_reports_per_run_outcome(sessions):
    """records_downloaded is cumulative and counts safety-window re-downloads; last_run does not."""
    client = FakeClient([Page([heart_rate()], None), Page([heart_rate()], None)])
    service = IncrementalSync(Settings(), sessions, client, {"heart-rate": REGISTRY["heart-rate"]})
    assert service.sequence == 0 and service.last_runs == {}
    await service.run("heart-rate")
    assert service.last_runs["heart-rate"] == {
        "sequence": 1,
        "inserted": 1,
        "updated": 0,
        "failed": False,
    }
    await service.run("heart-rate")
    assert service.last_runs["heart-rate"] == {
        "sequence": 2,
        "inserted": 0,
        "updated": 0,
        "failed": False,
    }

    class BrokenClient:
        async def list_data_points(self, spec, start, end, page_token=None):
            raise RuntimeError("Google erişimi reddedildi")

    service.client = BrokenClient()
    with pytest.raises(RuntimeError):
        await service.run("heart-rate")
    assert service.last_runs["heart-rate"] == {
        "sequence": 3,
        "inserted": 0,
        "updated": 0,
        "failed": True,
    }
    async with sessions() as session:
        state = await session.get(SyncState, "heart-rate")
    assert state.records_downloaded == 2
    runtime = SimpleNamespace(incremental=service)
    payload = _serialize(state, runtime)
    assert payload["status"] == JobStatus.failed
    assert payload["last_run"] == service.last_runs["heart-rate"]
    state.data_type = "steps"
    assert _serialize(state, runtime)["last_run"] is None


class RecordingClient:
    """Serves pages per window and records every (start, end, token) request."""

    def __init__(self, pages_by_start, fail_starts=()):
        self.pages_by_start = pages_by_start
        self.fail_starts = set(fail_starts)
        self.calls = []

    async def list_data_points(self, spec, start, end, page_token=None):
        self.calls.append((start, end, page_token))
        if start in self.fail_starts:
            raise RuntimeError("temporary Google failure")
        return self.pages_by_start[start][page_token]


def heart_rate_at(name, timestamp):
    return {
        "name": f"users/1/dataTypes/heart-rate/dataPoints/{name}",
        "heartRate": {
            "sampleTime": {"physicalTime": timestamp, "utcOffset": "0s"},
            "beatsPerMinute": 60,
        },
    }


OLD_START = datetime(2026, 1, 1, tzinfo=UTC)
OLD_END = datetime(2026, 1, 2, tzinfo=UTC)
NEW_START = datetime(2026, 8, 17, 10, tzinfo=UTC)
NEW_END = datetime(2026, 8, 17, 11, tzinfo=UTC)


async def interrupted_state(sessions, last_success=None):
    async with sessions.begin() as session:
        session.add(
            SyncState(
                data_type="heart-rate",
                status=JobStatus.failed,
                last_requested_from=OLD_START,
                last_requested_to=OLD_END,
                last_page_token="old-page-2",
                last_successful_sync=last_success,
            )
        )


@pytest.mark.parametrize("limit", [1, 1000])
async def test_resume_finishes_old_window_then_fetches_requested_window(
    sessions, monkeypatch, limit
):
    from app.google_health import pagination
    from app.google_health.pagination import PaginationPaused

    monkeypatch.setattr(pagination, "MAX_PAGES_PER_CALL", limit)
    current = datetime(2026, 8, 18, tzinfo=UTC)
    await interrupted_state(sessions, last_success=current)
    client = RecordingClient(
        {
            OLD_START: {"old-page-2": Page([heart_rate_at("old", "2026-01-01T12:00:00Z")], None)},
            NEW_START: {None: Page([heart_rate_at("new", "2026-08-17T10:30:00Z")], None)},
        }
    )
    service = IncrementalSync(Settings(), sessions, client, {"heart-rate": REGISTRY["heart-rate"]})

    if limit == 1:
        with pytest.raises(PaginationPaused):
            await service.run("heart-rate", NEW_START, NEW_END)
        assert client.calls == [(OLD_START, OLD_END, "old-page-2")]
    stats = await service.run("heart-rate", NEW_START, NEW_END)

    assert client.calls == [(OLD_START, OLD_END, "old-page-2"), (NEW_START, NEW_END, None)]
    assert stats.inserted == (1 if limit == 1 else 2)
    async with sessions() as session:
        names = set(await session.scalars(select(RawHealthData.google_data_point_id)))
        state = await session.get(SyncState, "heart-rate")
    assert names == {
        "users/1/dataTypes/heart-rate/dataPoints/old",
        "users/1/dataTypes/heart-rate/dataPoints/new",
    }
    assert state.status == JobStatus.completed
    assert state.last_page_token is None
    assert state.last_requested_from.replace(tzinfo=UTC) == NEW_START
    assert state.last_successful_sync.replace(tzinfo=UTC) == current


async def test_pending_first_page_precedes_a_different_requested_window(sessions):
    await interrupted_state(sessions)
    async with sessions.begin() as session:
        state = await session.get(SyncState, "heart-rate")
        state.status, state.last_page_token = JobStatus.pending, None
    client = RecordingClient(
        {
            OLD_START: {None: Page([], None)},
            NEW_START: {None: Page([], None)},
        }
    )
    service = IncrementalSync(Settings(), sessions, client, {"heart-rate": REGISTRY["heart-rate"]})
    await service.run("heart-rate", NEW_START, NEW_END)
    assert client.calls == [(OLD_START, OLD_END, None), (NEW_START, NEW_END, None)]


async def test_webhook_element_completes_only_after_its_own_window(sessions):
    await interrupted_state(sessions)
    async with sessions.begin() as session:
        session.add(
            OAuthToken(subject="me", encrypted_refresh_token="encrypted", health_user_id="user-1")
        )
    client = RecordingClient(
        {
            OLD_START: {"old-page-2": Page([], None)},
            NEW_START: {None: Page([heart_rate_at("new", "2026-08-17T10:30:00Z")], None)},
        },
        fail_starts={NEW_START},
    )
    incremental = IncrementalSync(
        Settings(), sessions, client, {"heart-rate": REGISTRY["heart-rate"]}
    )
    processor = WebhookBatchProcessor(sessions, incremental, {"heart-rate"})
    payload = {
        "data": {
            "dataType": "heart-rate",
            "healthUserId": "user-1",
            "operation": "UPSERT",
            "intervals": [
                {
                    "physicalTimeInterval": {
                        "startTime": "2026-08-17T10:00:00Z",
                        "endTime": "2026-08-17T11:00:00Z",
                    }
                }
            ],
        }
    }
    result = await processor.ingest_verified(b'{"signed":1}', payload)

    first = await processor.process_batch(result.batch_hash)
    async with sessions() as session:
        element = await session.scalar(select(WebhookNotification))
    # The resumed window succeeded, but the element's own window failed.
    assert first.failed_elements == 1
    assert element.status == "failed"
    assert (element.sync_attempt_count, element.sync_success_count) == (1, 0)

    client.fail_starts.clear()
    second = await processor.process_batch(result.batch_hash)
    async with sessions() as session:
        element = await session.scalar(select(WebhookNotification))
        stored = await session.scalar(select(func.count(RawHealthData.id)))
    assert second.completed_elements == 1
    assert element.status == "completed"
    assert (element.sync_attempt_count, element.sync_success_count) == (2, 1)
    assert stored == 1
    assert client.calls == [
        (OLD_START, OLD_END, "old-page-2"),
        (NEW_START, NEW_END, None),
        (NEW_START, NEW_END, None),
    ]


async def test_a_resumed_round_keeps_the_start_of_its_first_page(sessions, monkeypatch):
    """Re-review 1 #1: pages fetched after a resume belong to the fetch that began before
    the crash; "seen in this fetch" means since that start (persisted in fetch_rounds)."""
    from app.models.state import FetchRound
    from app.sync import incremental

    now = [datetime(2026, 10, 19, 12, tzinfo=UTC)]

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003 - signature of datetime.now
            return now[0]

    monkeypatch.setattr(incremental, "datetime", Frozen)
    starts = []
    original = incremental.store_page

    async def recording(session, spec, page, **options):
        starts.append(options.get("fetch_started"))
        return await original(session, spec, page, **options)

    monkeypatch.setattr(incremental, "store_page", recording)

    class Crashing:
        async def list_data_points(self, spec, start, end, page_token=None):
            if page_token is None:
                return Page([], "p2")
            raise RuntimeError("process restarted")

    specs = {"steps": REGISTRY["steps"]}
    with pytest.raises(RuntimeError):
        await IncrementalSync(Settings(), sessions, Crashing(), specs).run("steps")
    first = now[0]
    now[0] = first + timedelta(minutes=30)
    client = FakeClient([Page([], None), Page([], None)])
    await IncrementalSync(Settings(), sessions, client, specs).run("steps")
    assert client.tokens == ["p2", None]  # the interrupted round first, then a new one
    assert [start.replace(tzinfo=UTC) for start in starts] == [first, first, now[0]]
    async with sessions() as session:
        row = await session.get(FetchRound, ("sync", "steps"))
    assert row is not None and row.started_at.replace(tzinfo=UTC) == now[0]


@pytest.mark.parametrize("lane", ["sync", "backfill"])
async def test_numeric_failures_commit_receipts_and_advance_pages(sessions, lane, monkeypatch):
    from app.google_health.capture import CapturedResponse
    from app.models.health import Weight
    from app.models.raw_versions import RawApiResponse
    from app.models.retrieval import RetrievalReceipt
    from app.parsers.common import canonical_json
    from app.services import storage
    from app.sync import backfill, incremental

    records = [numeric_point(value=value) for value in (*BAD_NUMBERS, "9999999999.995", "71000")]
    body = canonical_json({"dataPoints": records}).encode()
    response = CapturedResponse(
        START, lane, None, "weight", "GET", "/synthetic", None, None, 1, 200, {}, body
    )
    client = Client(Page(records, "p2", responses=[response]), Page([], None))
    results = []

    async def storing(*args, **kwargs):
        stats = await storage.store_page(*args, **kwargs)
        results.append(stats)
        return stats

    monkeypatch.setattr(incremental if lane == "sync" else backfill, "store_page", storing)
    await Runner(sessions, lane, "weight").run(client)
    assert client.tokens == [None, "p2"]
    assert sum(s.normalization_skipped for s in results) == len(records) - 1
    assert sum(s.normalized for s in results) == 1
    async with sessions() as session:
        raw = list(await session.scalars(select(RawHealthData)))
        assert len(raw) == len(records) and all(row.raw_json in records for row in raw)
        archived = (await session.scalars(select(RawApiResponse))).one()
        assert archived.body == body and all(row.response_id == archived.id for row in raw)
        assert len(list(await session.scalars(select(Weight)))) == 1
        receipt = (await session.scalars(select(RetrievalReceipt))).one()
        events = list(await session.scalars(select(RetrievalEvent).order_by(RetrievalEvent.id)))
        assert receipt.lane == lane and [e.kind for e in events] == ["page", "page", "completed"]
        assert [e.returned_count for e in events] == [len(records), 0, 0]
        assert all(e.unplaced_count == 0 for e in events)
