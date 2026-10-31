"""Synthetic Google responses shared by retrieval and API behavior tests."""

import uuid
from datetime import UTC, datetime

from app.core.config import Settings
from app.google_health.client import Page
from app.google_health.data_types import REGISTRY
from app.google_health.pagination import PageBudget
from app.models.state import BackfillJob
from app.sync.backfill import BackfillService
from app.sync.incremental import IncrementalSync

START = datetime(2026, 9, 1, tzinfo=UTC)
END = datetime(2026, 9, 2, tzinfo=UTC)
OVERFLOW_TIMES = ("0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00")
BAD_DATES = [
    {**dict(year=2026, month=9, day=1), key: value}
    for key, whole in (("year", 2026), ("month", 9), ("day", 1))
    for value in (True, False, whole + 0.9)
]
BAD_TIMES = [("heart-rate", at) for at in OVERFLOW_TIMES] + [
    *[("daily-resting-heart-rate", day) for day in BAD_DATES],
    ("daily-resting-heart-rate", {"year": 10**100, "month": 1, "day": 1}),
]


def point(at="2026-09-01T12:00:00Z", kind="heart-rate", *, named=True, **payload):
    spec = REGISTRY[kind]
    fields = {"sampleTime": {"physicalTime": at}, "beatsPerMinute": 60}
    if kind in {"steps", "sleep"}:
        end = "2026-09-01T13:00:00Z" if at == "2026-09-01T12:00:00Z" else at
        fields = {"interval": {"startTime": at, "endTime": end}, "duration": "3600s"}
        if kind == "steps":
            fields = {**fields, "count": 42}
    elif kind == "daily-resting-heart-rate":
        fields = {"date": at[:10] if isinstance(at, str) else at, "beatsPerMinute": 60}
    elif kind == "weight":
        fields = {"sampleTime": {"physicalTime": at}, "weightGrams": 70000}
    record = {
        "name": f"users/example/dataTypes/{kind}/dataPoints/{uuid.uuid4()}",
        spec.json_key: {**fields, **payload},
    }
    return record if named else {spec.json_key: record[spec.json_key]}


class Client:
    def __init__(self, *pages):
        self.pages = iter(pages or [Page([], None)])
        self.tokens = []
        self.kept = []

    async def warm_up(self):
        pass

    async def list_data_points(self, spec, start, end, page_token=None):
        self.tokens.append(page_token)
        item = next(self.pages)
        if isinstance(item, BaseException):
            raise item
        return item

    async def keep_after(self, responses, failure):
        self.kept.append((responses, failure))


class Runner:
    def __init__(self, sessions, lane, kind="heart-rate", start=START, end=END):
        self.sessions, self.lane, self.kind = sessions, lane, kind
        self.start, self.end = start, end
        self.job_id = None

    async def run(self, client):
        specs = {self.kind: REGISTRY[self.kind]}
        if self.lane == "sync":
            return await IncrementalSync(Settings(), self.sessions, client, specs).run(
                self.kind, self.start, self.end
            )
        if self.job_id is None:
            self.job_id = uuid.uuid4()
            async with self.sessions.begin() as session:
                session.add(
                    BackfillJob(
                        id=self.job_id,
                        data_type=self.kind,
                        window_start=self.start,
                        window_end=self.end,
                    )
                )
        # The production per-window step: select this runnable job and fetch it.
        service = BackfillService(Settings(), self.sessions, client, specs)
        return await service._run_next_window(self.kind, PageBudget())
