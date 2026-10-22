"""Source-deletion marking and startup recovery against PostgreSQL timestamptz columns."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.google_health.client import Page
from app.google_health.data_types import REGISTRY
from app.models.state import FetchRound, JobStatus, RawHealthData, SyncState, fetch_round_start
from app.sync.incremental import INTERRUPTED_ERROR, STALE_RUNNING_AFTER, IncrementalSync

pytestmark = pytest.mark.integration

PLUS_THREE = timezone(timedelta(hours=3))
# 2026-08-01T00:00Z .. 2026-08-02T00:00Z, deliberately passed with a +03:00 offset.
WINDOW_START = datetime(2026, 8, 1, 3, tzinfo=PLUS_THREE)
WINDOW_END = datetime(2026, 8, 2, 3, tzinfo=PLUS_THREE)


def steps(name: str, start: str) -> dict:
    begin = datetime.fromisoformat(start)
    return {
        "name": f"users/me/dataTypes/steps/dataPoints/{name}",
        "steps": {
            "interval": {
                "startTime": start,
                "endTime": (begin + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            },
            "count": "10",
        },
    }


def sleep(name: str, start: str, end: str) -> dict:
    return {
        "name": f"users/me/dataTypes/sleep/dataPoints/{name}",
        "sleep": {"interval": {"startTime": start, "endTime": end}},
    }


class QueueClient:
    def __init__(self) -> None:
        self.responses: list[Page] = []

    async def list_data_points(self, spec, start, end, page_token=None):
        return self.responses.pop(0)


async def deleted(sessions, data_type: str) -> tuple[int, dict[str, datetime]]:
    async with sessions() as session:
        rows = (
            await session.scalars(select(RawHealthData).where(RawHealthData.data_type == data_type))
        ).all()
    marks = {
        row.google_data_point_id.rsplit("/", 1)[1]: row.source_deleted_at
        for row in rows
        if row.source_deleted_at is not None
    }
    return len(rows), marks


async def test_refetch_marks_missing_records_by_timestamptz_scope(pg_sessions) -> None:
    client = QueueClient()
    specs = {"steps": REGISTRY["steps"], "sleep": REGISTRY["sleep"]}
    service = IncrementalSync(Settings(_env_file=None, app_env="test"), pg_sessions, client, specs)
    at_start = steps("at-start", "2026-08-01T00:00:00Z")
    middle = steps("middle", "2026-08-01T12:00:00Z")
    last_minute = steps("last-minute", "2026-08-01T23:59:00Z")
    at_end = steps("at-end", "2026-08-02T00:00:00Z")  # Half-open window: outside.

    client.responses = [Page([at_start, middle, last_minute, at_end], None)]
    await service.run("steps", WINDOW_START, WINDOW_END)
    assert await deleted(pg_sessions, "steps") == (4, {})

    before = datetime.now(UTC)
    client.responses = [Page([middle], "next"), Page([], None)]
    stats = await service.run("steps", WINDOW_START, WINDOW_END)
    count, marks = await deleted(pg_sessions, "steps")
    assert stats.source_deleted == 2
    assert (count, set(marks)) == (4, {"at-start", "last-minute"})
    assert all(mark.tzinfo is not None and mark >= before for mark in marks.values())

    # Records that come back are live again; nothing was deleted along the way.
    client.responses = [Page([at_start, middle, last_minute], None)]
    stats = await service.run("steps", WINDOW_START, WINDOW_END)
    assert stats.source_deleted == 0
    assert await deleted(pg_sessions, "steps") == (4, {})

    # Sleep is scoped by its end time rather than its start.
    inside = sleep("inside", "2026-07-31T22:00:00Z", "2026-08-01T06:00:00Z")
    ends_at_window_end = sleep("ends-at-end", "2026-08-01T17:00:00Z", "2026-08-02T00:00:00Z")
    client.responses = [Page([inside, ends_at_window_end], None)]
    await service.run("sleep", WINDOW_START, WINDOW_END)
    client.responses = [Page([], None)]
    stats = await service.run("sleep", WINDOW_START, WINDOW_END)
    count, marks = await deleted(pg_sessions, "sleep")
    assert stats.source_deleted == 1
    assert (count, set(marks)) == (2, {"inside"})

    async with pg_sessions() as session:
        state = await session.get(SyncState, "steps")
    assert state.status == JobStatus.completed
    assert state.last_page_token is None
    assert state.last_requested_from == WINDOW_START


async def test_startup_recovery_fails_running_sync_states(pg_sessions) -> None:
    async with pg_sessions.begin() as session:
        session.add_all(
            [
                SyncState(
                    data_type="steps",
                    status=JobStatus.running,
                    last_page_token="resume-me",
                    last_requested_from=WINDOW_START,
                    last_requested_to=WINDOW_END,
                    updated_at=datetime.now(UTC) - STALE_RUNNING_AFTER - timedelta(minutes=1),
                ),
                SyncState(data_type="sleep", status=JobStatus.completed),
                SyncState(data_type="weight", status=JobStatus.abandoned),
                # Fresh heartbeat: possibly a live run in another process.
                SyncState(data_type="heart-rate", status=JobStatus.running),
            ]
        )
    service = IncrementalSync(
        Settings(_env_file=None, app_env="test"), pg_sessions, None, dict(REGISTRY)
    )

    assert await service.recover_interrupted() == 1
    assert await service.recover_interrupted() == 0

    async with pg_sessions() as session:
        states = {row.data_type: row for row in await session.scalars(select(SyncState))}
    assert states["steps"].status == JobStatus.failed
    assert states["steps"].last_error == INTERRUPTED_ERROR
    assert states["steps"].last_page_token == "resume-me"
    assert states["steps"].last_requested_to == WINDOW_END
    assert states["sleep"].status == JobStatus.completed
    assert states["weight"].status == JobStatus.abandoned
    assert states["heart-rate"].status == JobStatus.running


@pytest.mark.parametrize("fresh", [False, True])
async def test_two_workers_starting_the_same_fetch_both_get_its_round(pg_sessions, fresh) -> None:
    """Review 8 AF: two workers beginning the same fetch while ``fetch_rounds`` has no row for
    it (right after migration 0012) both inserted one, and the later commit failed on the
    primary key. The second insert now waits for the first and does nothing; the worker reads
    the one row: its start when resuming, its own ``now`` when it begins at the first page."""
    first_now = datetime(2026, 8, 1, tzinfo=UTC)
    second_now = first_now + timedelta(minutes=5)

    async def second() -> datetime:
        async with pg_sessions.begin() as session:
            return await fetch_round_start(session, "sync", "steps", fresh=fresh, now=second_now)

    async with pg_sessions.begin() as session:
        first = await fetch_round_start(session, "sync", "steps", fresh=True, now=first_now)
        # The other worker starts while this transaction is still open.
        other = asyncio.create_task(second())
        await asyncio.sleep(0.5)
    assert first == first_now
    assert await other == (second_now if fresh else first_now)

    async with pg_sessions() as session:
        rows = (await session.scalars(select(FetchRound))).all()
    assert [(row.kind, row.key) for row in rows] == [("sync", "steps")]
    assert rows[0].started_at == (second_now if fresh else first_now)
