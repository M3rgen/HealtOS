"""Current Google payloads govern step values; retained normalized rows are history."""

import csv
import io
from datetime import datetime, timedelta
from itertools import product

import httpx
import pytest
from derived_records import DAY, PHONE, T0, snapshot, steps, store
from fastapi import FastAPI
from retrieval_records import Client, Runner
from sqlalchemy import select

from app.api import dashboard, sync
from app.core.config import Settings
from app.db.session import get_session
from app.google_health.client import Page
from app.models.health import Steps
from app.models.raw_versions import RawHealthDataVersion
from app.models.state import RawHealthData
from app.services import codex_health, retrieval_coverage

LAST = DAY + timedelta(days=2)
DATES = [(DAY + timedelta(days=i)).isoformat() for i in range(3)]
QUERY = {"start_date": DATES[0], "end_date": DATES[-1], "timezone": "UTC"}


@pytest.fixture
async def api(sessions, monkeypatch):
    class Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return (T0 + timedelta(days=3)).astimezone(tz)

    monkeypatch.setattr(dashboard, "datetime", Now)
    monkeypatch.setattr(codex_health, "SessionLocal", sessions)
    app = FastAPI()
    app.state.settings = Settings(_env_file=None)
    app.include_router(dashboard.router)
    app.include_router(sync.coverage_router)

    async def session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        yield client


async def outputs(api, sessions, expected):
    async with sessions() as session:
        before = await snapshot(session)
    response = await api.get("/api/dashboard", params={"end_date": LAST.isoformat(), "days": 3})
    assert response.status_code == 200
    data = response.json()
    assert [row["steps"] for row in reversed(data["days"])] == expected
    # Check the old day's hourly chart too, even after the raw interval moved/disappeared.
    for day, value in zip(DATES, expected, strict=True):
        chart = (await api.get("/api/dashboard", params={"end_date": day, "days": 1})).json()
        assert chart["charts"]["steps"] == [value, *([None] * 23)]
    history = (await api.get("/api/dashboard/history", params={"metric": "steps"})).json()
    by_day = {row["date"]: row["value"] for row in history["days"]}
    assert [by_day[day] for day in DATES] == expected
    response = await api.get("/api/dashboard/export.csv", params={"end_date": DATES[-1], "days": 3})
    exported = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
    assert [row["steps"] for row in reversed(exported)] == [
        "" if value is None else str(value) for value in expected
    ]
    daily = await codex_health.read_health({"action": "daily", **QUERY})
    assert [row["steps"] for row in reversed(daily["days"])] == expected
    card = await codex_health.read_health({"action": "coverage", "data_type": "steps", **QUERY})
    [metric] = card["metrics"]
    present = [day for day, value in zip(DATES, expected, strict=True) if value is not None]
    assert metric["value_days"] == metric["sampling"]["sample_count"] == len(present)
    assert metric["days_without_value"] == 3 - len(present)
    assert sorted(row["first_date"] for row in metric["source_regimes"]) == present
    assert all(row["value_days"] == row["record_count"] == 1 for row in metric["source_regimes"])
    response = await api.get("/api/sync/coverage", params={**QUERY, "data_type": "steps"})
    assert response.status_code == 200, response.text
    [kind] = response.json()["types"]
    assert [row["has_value"] for row in kind["days"]] == [v is not None for v in expected]
    # Completed retrieval remains proof of the fetch, even when a correction has no value.
    assert [row["retrieval"] for row in kind["days"]] == [
        "completed_with_records",
        "completed_empty",
        "completed_with_records",
    ]
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize(
    ("count", "interval"),
    list(product((None, "invalid", True), ("same", "moved", "lost")))
    + [(120, loss) for loss in ("startTime", "endTime", "lost")],
)
async def test_invalid_correction_and_recovery_across_all_readers(api, sessions, count, interval):
    original = steps(count=120)
    sound = steps("phone", count=30, start=T0 + timedelta(days=2), source=PHONE)
    await Runner(sessions, "sync", "steps", T0, T0 + timedelta(days=3)).run(
        Client(Page([original, sound], None))
    )
    await outputs(api, sessions, [120, None, 30])
    corrected = steps(count=count, start=T0 + timedelta(days=interval == "moved"))
    if count is None:
        corrected["steps"].pop("count")  # truly absent, rather than JSON null
    if interval == "lost":
        corrected["steps"].pop("interval")
    elif interval in {"startTime", "endTime"}:
        corrected["steps"]["interval"].pop(interval)
    stats = await store(sessions, "steps", corrected)
    assert stats.updated == stats.versions_kept == 1
    # A start-only payload takes the normalizer's generic scalar fallback; Steps stays old.
    assert stats.normalization_skipped == (interval != "endTime")
    async with sessions() as session:
        raw = await session.scalar(
            select(RawHealthData).where(RawHealthData.google_data_point_id == original["name"])
        )
        assert raw.raw_json == corrected
        old = await session.scalar(
            select(Steps).where(Steps.google_data_point_id == raw.google_data_point_id)
        )
        assert old.count == 120 and old.start_time.date() == DAY
        [version] = list(await session.scalars(select(RawHealthDataVersion)))
        assert version.raw_json == original and version.version == 1
    await outputs(api, sessions, [None, None, 30])
    # Both aliases supported by normalization, including measured zero, recover normally.
    for alias, value in (("countSum", 0), ("steps", 45)):
        recovery = steps(start=T0 + timedelta(days=1))
        recovery["steps"] = {"interval": recovery["steps"]["interval"], alias: value}
        await store(sessions, "steps", recovery)
        await outputs(api, sessions, [None, value, 30])
    async with sessions() as session:
        versions = list(await session.scalars(select(RawHealthDataVersion).order_by("version")))
        assert [row.raw_json for row in versions[:2]] == [original, corrected]
        assert [row.version for row in versions] == [1, 2, 3]


async def test_coverage_read_budget_follows_current_interval(api, sessions, monkeypatch):
    await store(sessions, "steps", steps())
    await store(sessions, "steps", steps(count=None, start=T0 + timedelta(days=1)))
    monkeypatch.setattr(retrieval_coverage, "MAX_RECORDS", 0)
    for day, status in ((DATES[0], 200), (DATES[1], 413)):
        response = await api.get(
            "/api/sync/coverage",
            params={**QUERY, "data_type": "steps", "start_date": day, "end_date": day},
        )
        assert response.status_code == status, response.text
        if status == 413:
            assert response.json()["detail"]["code"] == "coverage_record_limit"
        else:
            assert response.json()["types"][0]["days"][0]["has_value"] is False


async def test_exclusions_follow_the_current_interval_of_a_moved_correction(api, sessions):
    await store(sessions, "steps", steps(count=120))
    # Invalid correction moved to the next day: the retained Steps row keeps the old day.
    await store(sessions, "steps", steps(count=None, start=T0 + timedelta(days=1)))
    async with sessions.begin() as session:
        row = await session.scalar(select(Steps))
        assert row.start_time.date() == DAY
        row.is_quarantined = True
    for day, count in ((DATES[0], 0), (DATES[1], 1), (DATES[2], 0)):
        response = await api.get(
            "/api/sync/coverage",
            params={**QUERY, "data_type": "steps", "start_date": day, "end_date": day},
        )
        assert response.status_code == 200, response.text
        [kind] = response.json()["types"]
        assert kind["exclusions"]["quarantined"] == count, day
        assert kind["days"][0]["has_value"] is False
