"""Real ingestion corrections stay consistent across every public sleep reader."""

import csv
import io
from datetime import datetime, timedelta

import httpx
import pytest
from derived_records import DAY, PHONE, T0, sleep, snapshot, stage, store, streams
from fastapi import FastAPI
from retrieval_records import Client, Runner
from sqlalchemy import select

from app.api import dashboard, sync
from app.core.config import Settings
from app.db.session import get_session
from app.google_health.client import Page
from app.models.health import SleepSession
from app.models.raw_versions import RawHealthDataVersion
from app.models.state import RawHealthData
from app.services import codex_health
from app.services.canonical_daily import bounds

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
    params = {"end_date": DATES[-1], "days": 3}
    data = (await api.get("/api/dashboard", params=params)).json()
    assert [row["sleep_seconds"] for row in reversed(data["days"])] == expected
    history = (await api.get("/api/dashboard/history", params={"metric": "sleep"})).json()
    values = {row["date"]: row["value"] for row in history["days"]}
    assert [values[day] for day in DATES] == expected
    assert history["earliest_date"] == next(
        day for day, value in zip(DATES, expected, strict=True) if value is not None
    )
    exported = (await api.get("/api/dashboard/export.csv", params=params)).text
    rows = list(csv.DictReader(io.StringIO(exported.lstrip("\ufeff"))))
    assert [
        float(row["sleep_seconds"]) if row["sleep_seconds"] else None for row in reversed(rows)
    ] == expected
    daily = await codex_health.read_health({"action": "daily", **QUERY})
    assert [row["sleep_seconds"] for row in reversed(daily["days"])] == expected
    # Rebuild the already registered derived days after each write. The canonical
    # reader already excludes undated corrections; its behavior must stay aligned.
    for i, value in enumerate(expected):
        derived = streams(daily["derived"], "sleep_seconds", i)
        assert sorted(row["value"] for row in derived) == (
            [0, 1800] if i == 2 else [] if value is None else [value]
        )
    card = await codex_health.read_health({"action": "coverage", "data_type": "sleep", **QUERY})
    [metric] = card["metrics"]
    present = sum(value is not None for value in expected)
    assert metric["value_days"] == present
    assert metric["days_without_value"] == 3 - present
    assert metric["sampling"]["sample_count"] == present + 1  # two sound sources on day three
    assert len(metric["source_regimes"]) == 2
    response = await api.get("/api/sync/coverage", params={**QUERY, "data_type": "sleep"})
    assert response.status_code == 200, response.text
    [kind] = response.json()["types"]
    assert [row["has_value"] for row in kind["days"]] == [v is not None for v in expected]
    assert [row["retrieval"] for row in kind["days"]] == [
        "completed_with_records",
        "completed_empty",
        "completed_with_records",
    ]
    assert len(kind["source_regimes"]) == 2
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize(
    "loss",
    [
        "interval",
        "interval-null",
        "interval-string",
        "startTime",
        "endTime",
        "bad-start",
        "bad-end",
        "equal-interval",
        "reverse-interval",
    ],
)
async def test_interval_loss_and_recovery_across_readers(api, sessions, loss):
    original = sleep(summary={"minutesAsleep": 120})
    sound = sleep(
        "sound",
        start=T0 + timedelta(days=2),
        end=T0 + timedelta(days=2, hours=1),
        source=PHONE,
        summary={"minutesAsleep": 30},
    )
    zero = sleep(
        "zero",
        start=T0 + timedelta(days=2, hours=2),
        summary={"minutesAsleep": 0},
        end=T0 + timedelta(days=2, hours=3),
    )
    await Runner(sessions, "sync", "sleep", T0, T0 + timedelta(days=3)).run(
        Client(Page([original, sound, zero], None))
    )
    await outputs(api, sessions, [7200, None, 1800])
    corrected = sleep(
        start=T0 + timedelta(days=1),
        end=T0 + timedelta(days=1, hours=2),
        # A nonfinite source number skips normalization even with both instants.
        summary={"minutesAsleep": 120, "minutesInSleepPeriod": "NaN"},
    )
    if loss in {"equal-interval", "reverse-interval"}:
        corrected["sleep"]["interval"]["endTime"] = (
            T0 + timedelta(days=1, hours=-int(loss == "reverse-interval"))
        ).isoformat()
    elif loss == "interval":
        corrected["sleep"].pop("interval")
    elif loss.startswith("interval-"):
        corrected["sleep"]["interval"] = None if loss.endswith("null") else "invalid"
    elif loss.startswith("bad-"):
        corrected["sleep"]["interval"]["startTime" if loss == "bad-start" else "endTime"] = (
            "invalid"
        )
    else:
        corrected["sleep"]["interval"].pop(loss)
    stats = await store(sessions, "sleep", corrected)
    assert stats.updated == stats.versions_kept == 1
    async with sessions() as session:
        raw = await session.scalar(
            select(RawHealthData).where(RawHealthData.google_data_point_id == original["name"])
        )
        retained = await session.scalar(
            select(SleepSession).where(SleepSession.google_data_point_id == original["name"])
        )
        assert raw.raw_json == corrected
        assert retained.start_time.date() == retained.end_time.date() == DAY
        assert retained.duration_seconds == 7200
        [version] = list(await session.scalars(select(RawHealthDataVersion)))
        assert version.raw_json == original and version.version == 1
    await outputs(api, sessions, [None, None, 1800])
    recovery = sleep(
        start=T0 + timedelta(hours=23),
        end=T0 + timedelta(days=1, hours=1),
        summary={"minutesAsleep": 0},
    )
    await store(sessions, "sleep", recovery)
    await outputs(api, sessions, [None, 0, 1800])
    async with sessions() as session:
        versions = list(await session.scalars(select(RawHealthDataVersion).order_by("version")))
        assert [row.raw_json for row in versions] == [original, corrected]


@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize("offset", [0, 1])
@pytest.mark.parametrize("asleep", [0, 60])
@pytest.mark.parametrize("timezone", ["UTC", "Pacific/Honolulu"])
async def test_skipped_normalization_uses_current_sleep_interval(
    api, sessions, initial, offset, asleep, timezone
):
    original = sleep(summary={"minutesAsleep": 120}, stages=[stage("DEEP", 1, 3)])
    if not initial:
        await store(sessions, "sleep", original)
    query = dict(QUERY, start_date=str(DAY - timedelta(days=1)), timezone=timezone)
    await codex_health.read_health({"action": "daily", **query})
    async with sessions() as session:
        retained = (await snapshot(session))["sleep_sessions"]
        assert bool(retained) is not initial
    left = T0 + timedelta(days=offset, hours=4)
    right = left + timedelta(hours=2)
    corrected = sleep(
        start=left,
        end=right,
        # A nonfinite source number skips normalization; readers never use this field.
        summary={"minutesAsleep": asleep, "minutesInSleepPeriod": "NaN"},
        stages=[{"type": "LIGHT", "startTime": left.isoformat(), "endTime": right.isoformat()}],
    )
    stats = await store(sessions, "sleep", corrected)
    assert stats.normalization_skipped == 1 and stats.versions_kept == int(not initial)
    async with sessions() as session:
        before = await snapshot(session)
        assert before["sleep_sessions"] == retained
        assert list(await session.scalars(select(RawHealthDataVersion.raw_json))) == (
            [] if initial else [original]
        )
        assert await session.scalar(select(RawHealthData.raw_json)) == corrected
    daily = await codex_health.read_health({"action": "daily", **query})
    for day in daily["derived"]["days"]:
        rows = streams({"days": [day]}, "sleep_seconds")
        first, stop = bounds(datetime.fromisoformat(day["date"]).date(), timezone)
        if not first <= right < stop:
            assert rows == []
            continue
        [row] = rows
        assert row["value"] == row["observations"][0]["value"] == asleep * 60
        observation = row["observations"][0]
        assert observation["start"] == observation["source_start"] == left.isoformat()
        assert observation["end"] == observation["source_end"] == right.isoformat()
        [span] = row["sleep"]["sessions"]
        assert (span["start"], span["end"], span["interval_seconds"]) == (
            left.isoformat(),
            right.isoformat(),
            7200,
        )
        assert row["sleep"]["timeline_stage_seconds"] == {"light": 7200}
        assert row["sleep"]["timeline"][0]["start"] == left.isoformat()
        assert row["sleep"]["timeline"][0]["end"] == right.isoformat()
        assert "from_normalized_columns" not in row["uncertainties"]
    for action in ("baseline", "analyze"):
        result = await codex_health.read_health(
            {"action": action, "metric": "sleep_seconds", **query}
        )
        assert [row["value"] for row in result["summaries"]] == [asleep * 60]
    async with sessions() as session:
        assert await snapshot(session) == before
