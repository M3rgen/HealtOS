"""A record jsonb cannot hold is set apart; its data type keeps synchronizing."""

import hashlib
import json

import httpx
import pytest
from raw_response_records import SPEC, body, client, steps, stored
from rejection_records import (
    BAD_NAME,
    END,
    KINDS,
    NEXT_NAME,
    START,
    bad_record,
    refuse_like_jsonb,
    wire,
)
from retrieval_records import Runner
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.core.timeutil import as_utc
from app.google_health.pagination import RepeatedPageTokenError
from app.models.raw_rejections import RawHealthDataRejection as Rejection
from app.models.raw_rejections import RetrievalEventRejection
from app.models.retrieval import RetrievalEvent
from app.models.state import BackfillJob, JobStatus, RawHealthData, SyncState
from app.services import raw_rejections, storage
from app.services.raw_history import verify_guards
from app.services.storage import store_records
from app.sync.incremental import IncrementalSync


def two_pages(first: bytes, calls: list, last: bytes | None = None):
    last = last or body(steps(2, name=NEXT_NAME))

    def handler(request):
        token = request.url.params.get("pageToken")
        calls.append(token)
        return httpx.Response(200, content=last if token else first)

    return handler


def sync(sessions, google):
    settings = Settings(_env_file=None, app_env="test")
    return IncrementalSync(settings, sessions, google, {"steps": SPEC})


async def count(sessions, model) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


@pytest.mark.parametrize("kind", KINDS)
async def test_an_unstorable_record_no_longer_blocks_its_data_type(
    sessions, monkeypatch, kind, caplog
):
    await refuse_like_jsonb(sessions)
    bad = bad_record(kind)
    first = wire(body(steps(1), bad, token="next"))
    calls: list = []
    google = client(two_pages(first, calls), sessions)
    incremental = sync(sessions, google)

    async def everything(session, spec, records, **_):
        return list(records), 0

    try:
        # Before: the same page fails every attempt and the type never advances (a lone
        # surrogate already fails hashing, before PostgreSQL sees it).
        with monkeypatch.context() as patch:
            patch.setattr(storage, "reject_unstorable", everything)
            for _ in range(2):
                with pytest.raises((DBAPIError, UnicodeEncodeError)):
                    await incremental.run("steps", START, END)
        async with sessions() as session:
            assert (await session.get(SyncState, "steps")).last_successful_sync is None
        assert await count(sessions, RawHealthData) == await count(sessions, Rejection) == 0

        stats = await incremental.run("steps", START, END)
        assert (stats.inserted, stats.rejected) == (2, 1)
        assert calls[-2:] == [None, "next"]
        async with sessions() as session:
            state = await session.get(SyncState, "steps")
            assert state.status == JobStatus.completed and state.last_page_token is None
            assert as_utc(state.last_successful_sync) == END
            names = set(await session.scalars(select(RawHealthData.google_data_point_id)))
            assert BAD_NAME not in names and len(names) == 2
            rejected = (await session.scalars(select(Rejection))).one()
            assert await verify_guards(session) == []
        responses = await stored(sessions)
        page = next(row for row in reversed(responses) if row.body == first)
        # The exchange keeps the bytes Google sent; the row points at it.
        assert (rejected.response_id, rejected.ordinal) == (page.id, 1)
        assert (rejected.source, rejected.context_id[:5]) == ("sync", "sync:")
        assert rejected.element_json.isascii()
        assert json.loads(rejected.element_json) == bad
        assert rejected.element_sha256 == hashlib.sha256(rejected.element_json).hexdigest()
        assert "private-marker" not in caplog.text
    finally:
        await google.http.aclose()


async def test_every_exchange_keeps_its_own_response_and_rejection_row(sessions):
    first = wire(body(steps(1), bad_record("overflow"), token="next"))
    google = client(two_pages(first, []), sessions)
    incremental = sync(sessions, google)
    try:
        for attempt in range(1, 4):
            stats = await incremental.run("steps", START, END)
            assert stats.rejected == 1 and stats.inserted == (2 if attempt == 1 else 0)
            # No response is reused: fetched_at, attempt and context stay per exchange.
            assert len(await stored(sessions)) == 2 * attempt
            assert await count(sessions, Rejection) == attempt
        async with sessions() as session:
            rows = list(await session.scalars(select(Rejection).order_by(Rejection.id)))
            assert len({row.response_id for row in rows}) == 3
            assert len({row.context_id for row in rows}) == 3
            assert len({row.element_sha256 for row in rows}) == 1
            assert (await raw_rejections.summary(session))["unacknowledged"] == 1
    finally:
        await google.http.aclose()


async def test_a_rejected_record_defers_source_deletion_until_a_clean_round(sessions):
    async with sessions.begin() as session:
        await store_records(session, SPEC, [steps(5, name=BAD_NAME)])
    rounds = [
        body(bad_record("nul"), steps(2, name=NEXT_NAME)),
        body(steps(2, name=NEXT_NAME)),
    ]
    google = client(lambda _: httpx.Response(200, content=rounds[0]), sessions)
    incremental = sync(sessions, google)
    try:
        stats = await incremental.run("steps", START, END)
        assert (stats.rejected, stats.source_deleted) == (1, 0)
        async with sessions() as session:
            old = await session.scalar(
                select(RawHealthData).where(RawHealthData.google_data_point_id == BAD_NAME)
            )
            # Neither marked deleted nor overwritten: the stored payload stays as it was.
            assert old.source_deleted_at is None and old.raw_json == steps(5, name=BAD_NAME)
        rounds.pop(0)
        stats = await incremental.run("steps", START, END)
        # Marking only waited: a round without rejections marks what Google no longer sends.
        assert (stats.rejected, stats.source_deleted) == (0, 1)
    finally:
        await google.http.aclose()


async def test_retrieval_counts_rejected_records_apart_from_unplaced_ones(sessions):
    google = client(two_pages(wire(body(steps(1), bad_record("nul"), token="next")), []), sessions)
    try:
        await sync(sessions, google).run("steps", START, END)
    finally:
        await google.http.aclose()
    async with sessions() as session:
        pages = list(
            await session.scalars(
                select(RetrievalEvent).where(RetrievalEvent.kind == "page").order_by("id")
            )
        )
        rows = await session.scalars(select(RetrievalEventRejection))
        counts = {row.event_id: row.rejected_count for row in rows}
    assert [(page.returned_count, page.unplaced_count) for page in pages] == [(2, 0), (1, 0)]
    # Google returned the rejected record at its time: coverage still sees it returned.
    assert pages[0].returned_times == [["2026-08-17T10:00:00+00:00", 2]]
    assert counts == {pages[0].id: 1}


async def test_a_cyclic_page_sets_its_unstorable_record_apart_and_counts_it(sessions):
    # #252 stores a cyclic page without a page event; #234 sets its jsonb-refused record
    # apart. The failure outcome then counts that rejection beside what Google returned.
    await refuse_like_jsonb(sessions)
    cyclic = wire(body(steps(2, name=NEXT_NAME), bad_record("nul"), token="repeat"))
    google = client(two_pages(body(steps(1), token="repeat"), [], cyclic), sessions)
    try:
        with pytest.raises(RepeatedPageTokenError):
            await sync(sessions, google).run("steps", START, END)
    finally:
        await google.http.aclose()
    async with sessions() as session:
        events = list(await session.scalars(select(RetrievalEvent).order_by(RetrievalEvent.id)))
        rows = await session.scalars(select(RetrievalEventRejection))
        counts = {row.event_id: row.rejected_count for row in rows}
        names = set(await session.scalars(select(RawHealthData.google_data_point_id)))
        state = await session.get(SyncState, "steps")
    assert [(e.kind, e.returned_count) for e in events] == [("page", 1), ("failed", 2)]
    assert counts == {events[1].id: 1}
    assert NEXT_NAME in names and BAD_NAME not in names
    assert await count(sessions, Rejection) == 1
    assert state.last_successful_sync is None


async def test_a_failed_page_rolls_back_its_rejections_but_keeps_the_exchange(
    sessions, monkeypatch
):
    google = client(lambda _: httpx.Response(200, content=body(bad_record(), steps(1))), sessions)

    async def broken(*_args, **_kwargs):
        raise RuntimeError("synthetic store failure")

    monkeypatch.setattr(storage, "_upsert_normalized_many", broken)
    try:
        with pytest.raises(RuntimeError, match="synthetic store failure"):
            await sync(sessions, google).run("steps", START, END)
    finally:
        await google.http.aclose()
    assert await count(sessions, Rejection) == await count(sessions, RawHealthData) == 0
    assert len(await stored(sessions)) == 1
    async with sessions() as session:
        failed = await session.scalar(
            select(RetrievalEvent.id).where(RetrievalEvent.kind == "failed")
        )
        assert await session.get(RetrievalEventRejection, failed) is not None


async def test_without_an_exchange_the_record_key_prevents_repeats(sessions):
    for _ in range(2):
        async with sessions.begin() as session:
            stats = await store_records(session, SPEC, [bad_record("surrogate"), steps(1)])
        assert (stats.rejected, stats.inserted + stats.unchanged) == (1, 1)
    async with sessions() as session:
        row = (await session.scalars(select(Rejection))).one()
    assert row.response_id is None and row.ordinal == 0
    assert json.loads(row.element_json) == bad_record("surrogate")
    async with sessions.begin() as session:
        # Another place is another record of the batch.
        await store_records(session, SPEC, [steps(1), bad_record("surrogate")])
    assert await count(sessions, Rejection) == 2


async def test_backfill_finishes_its_window_with_a_rejected_record(sessions):
    google = client(two_pages(body(bad_record("nul_key"), steps(1), token="next"), []), sessions)
    runner = Runner(sessions, "backfill", "steps", START, END)
    try:
        assert await runner.run(google)
    finally:
        await google.http.aclose()
    async with sessions() as session:
        job = await session.get(BackfillJob, runner.job_id)
        assert job.status == JobStatus.completed and job.page_token is None
        rejected = (await session.scalars(select(Rejection))).one()
    assert rejected.source == "backfill" and rejected.context_id == f"backfill:{runner.job_id}"
    assert await count(sessions, RawHealthData) == 2
