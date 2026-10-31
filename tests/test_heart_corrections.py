"""Real ingestion corrections never revive retained heart-rate measurements."""

from datetime import timedelta

import httpx
import pytest
from derived_records import BAND, DAY, PHONE, T0, point, snapshot, store
from fastapi import FastAPI
from sqlalchemy import select, update

from app.api.dashboard import router
from app.db.session import get_session
from app.models.health import HeartRate
from app.models.raw_versions import RawHealthDataVersion
from app.models.state import RawHealthData
from app.services import codex_health, current_heart
from app.services.dashboard import dashboard_data


def heart(name="one", at=T0, source=PHONE, **values):
    return point(
        "heart-rate", name, {"sampleTime": {"physicalTime": at.isoformat()}, **values}, source
    )


@pytest.fixture
async def api(sessions, monkeypatch):
    monkeypatch.setattr(codex_health, "SessionLocal", sessions)
    app = FastAPI()
    app.include_router(router)

    async def session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        yield client


async def outputs(api, sessions, expected, timezone="UTC", first=DAY):
    async with sessions() as session:
        before = await snapshot(session)
    for offset, hours in enumerate(expected):
        day = first + timedelta(days=offset)
        chart = [hours.get(hour) for hour in range(24)]
        async with sessions() as session:
            result = await dashboard_data(session, day, 1, timezone)
            assert result["charts"]["heart_rate"] == chart
        query = dict(end_date=str(day), timezone=timezone)
        response = await api.get("/api/dashboard", params={**query, "days": 1})
        assert response.status_code == 200, response.text
        assert response.json()["charts"]["heart_rate"] == chart
        daily = await codex_health.read_health({"action": "daily", "start_date": str(day), **query})
        assert daily["charts"]["heart_rate"] == chart
    intraday = await codex_health.read_health(
        dict(action="intraday", data_type="heart-rate", start_date=str(first), **query)
    )
    evidence = [e for row in intraday["summaries"] for e in row["evidence"]]
    if all(77 not in hours.values() for hours in expected):
        assert all(e["value"] != 77 for e in evidence)
    async with sessions() as session:
        assert await snapshot(session) == before
    return evidence


@pytest.mark.parametrize("moved", [False, True])
@pytest.mark.parametrize(
    "loss",
    [
        "missing",
        "null",
        "text",
        "boolean",
        "sample",
        "time",
        "bad",
        "null-time",
        "null-sample",
        "boolean-time",
    ],
)
async def test_invalid_correction_and_alias_recovery(api, sessions, loss, moved):
    original = heart(beatsPerMinute=77)
    sound = heart("sound", at=T0 + timedelta(days=2, hours=4), source=BAND, beatsPerMinute=31)
    await store(sessions, "heart-rate", original, sound)
    await outputs(api, sessions, [{0: 77}, {}, {4: 31}])
    corrected = heart(at=T0 + timedelta(days=int(moved)), beatsPerMinute=77)
    payload = corrected["heartRate"]
    if loss in {"missing", "null", "text", "boolean"}:
        payload.pop("beatsPerMinute")
        if loss != "missing":
            payload["beatsPerMinute"] = {"null": None, "text": "invalid", "boolean": True}[loss]
    elif loss == "sample":
        payload.pop("sampleTime")
    elif loss == "time":
        payload["sampleTime"].pop("physicalTime")
    elif loss == "null-sample":
        payload["sampleTime"] = None
    else:
        payload["sampleTime"]["physicalTime"] = {
            "bad": "invalid",
            "null-time": None,
            "boolean-time": True,
        }[loss]
    stats = await store(sessions, "heart-rate", corrected)
    assert stats.updated == stats.versions_kept == stats.normalization_skipped == 1
    async with sessions() as session:
        raw = await session.scalar(
            select(RawHealthData).where(RawHealthData.google_data_point_id == original["name"])
        )
        old = await session.scalar(
            select(HeartRate).where(HeartRate.google_data_point_id == original["name"])
        )
        assert raw.raw_json == corrected
        assert old.bpm == 77 and old.timestamp.date() == DAY
        [version] = list(await session.scalars(select(RawHealthDataVersion)))
        assert version.raw_json == original and version.version == 1
    await outputs(api, sessions, [{}, {}, {4: 31}])
    for key, alias, value in (
        ("heartRate", "beatsPerMinute", 0),
        ("heart_rate", "bpm", 42),
        ("heart-rate", "value", 63),
    ):
        recovery = heart(at=T0 + timedelta(days=1, hours=2))
        recovery[key] = {**recovery.pop("heartRate"), alias: value}
        await store(sessions, "heart-rate", recovery)
        evidence = await outputs(api, sessions, [{}, {2: value}, {4: 31}])
        assert any(e["record"] == original["name"] and e["value"] == value for e in evidence)
    async with sessions() as session:
        versions = list(await session.scalars(select(RawHealthDataVersion).order_by("version")))
        assert [row.raw_json for row in versions[:2]] == [original, corrected]
        assert [row.version for row in versions] == [1, 2, 3, 4]


@pytest.mark.parametrize(
    ("timezone", "at", "hours"),
    [
        ("Europe/Istanbul", T0 - timedelta(hours=3), [{0: 40}, {}]),
        ("Europe/Istanbul", T0 + timedelta(hours=21), [{}, {0: 40}]),
        ("America/New_York", T0 + timedelta(hours=4), [{0: 40}, {}]),
        ("UTC", T0 + timedelta(days=1), [{}, {0: 40}]),
    ],
)
async def test_move_across_civil_day_boundaries(api, sessions, timezone, at, hours):
    await store(sessions, "heart-rate", heart(at=T0 + timedelta(hours=12), beatsPerMinute=77))
    recovery = heart(beatsPerMinute=40)
    recovery["heartRate"].pop("sampleTime")
    recovery.update(startTime=at.isoformat())  # temporal's top-level fallback
    await store(sessions, "heart-rate", recovery)
    await outputs(api, sessions, hours, timezone)


@pytest.mark.parametrize("state", ["deleted", "raw-quarantine", "normalized-quarantine"])
async def test_source_filters_still_apply_to_current_samples(api, sessions, state):
    original = heart(beatsPerMinute=77)
    await store(sessions, "heart-rate", original)
    await store(sessions, "heart-rate", heart(at=T0 + timedelta(hours=1), bpm=50))
    model = HeartRate if state == "normalized-quarantine" else RawHealthData
    values = {"source_deleted_at": T0} if state == "deleted" else {"is_quarantined": True}
    async with sessions() as session, session.begin():
        await session.execute(update(model).values(**values))
    await outputs(api, sessions, [{}])
    async with sessions() as session:
        before = await snapshot(session)
        for strict in (False, True):
            result = await dashboard_data(
                session, DAY, 1, "UTC", include_source_deleted=True, valid_sources_only=strict
            )
            expected = 50 if state == "deleted" and not strict else None
            assert result["charts"]["heart_rate"] == [None, expected, *([None] * 22)]
        assert await snapshot(session) == before


async def test_read_window_uses_current_source_time(sessions, monkeypatch):
    await store(sessions, "heart-rate", heart(beatsPerMinute=77))
    corrected = heart(at=T0 + timedelta(days=1), beatsPerMinute=None)
    await store(sessions, "heart-rate", corrected)
    parsed = []
    reader = current_heart.current_heart_rate

    def tracked(record):
        parsed.append(record)
        return reader(record)

    monkeypatch.setattr(current_heart, "current_heart_rate", tracked)
    async with sessions() as session:
        await dashboard_data(session, DAY, 1, "UTC")
        assert parsed == []  # retained normalized time must not select this payload
        await dashboard_data(session, DAY + timedelta(days=1), 1, "UTC")
        assert parsed == [corrected]
