from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import app.api.dashboard as dashboard_api
import app.services.dashboard as dashboard_service
from app.db.session import get_session
from app.models.health import SleepSession, Steps
from app.models.state import RawHealthData


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        instant = datetime(2026, 9, 30, 0, 30, tzinfo=UTC)
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(dashboard_api, "datetime", FixedDateTime)
    monkeypatch.setattr(dashboard_service, "datetime", FixedDateTime)


@pytest_asyncio.fixture
async def client(sessions):
    application = FastAPI()
    application.include_router(dashboard_api.router)

    async def override_session():
        async with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as connection:
        yield connection


def records(metric, day, identifier, *, quarantined=False, deleted=False, normalized_bad=False):
    stamp = datetime.combine(day, datetime.min.time(), UTC)
    kind = {"steps": "steps", "sleep": "sleep", "heart": "daily-resting-heart-rate"}[metric]
    start, end = stamp + timedelta(hours=12), stamp + timedelta(hours=13)
    normalized = []
    if metric == "steps":
        normalized = [
            Steps(
                google_data_point_id=identifier,
                start_time=start,
                end_time=end,
                count=600,
                is_quarantined=normalized_bad,
            )
        ]
    elif metric == "sleep":
        start, end = stamp - timedelta(hours=2), stamp + timedelta(hours=6)
        normalized = [
            SleepSession(
                google_data_point_id=identifier,
                start_time=start,
                end_time=end,
                duration_seconds=28800,
                awake_seconds=1800,
                is_quarantined=normalized_bad,
            )
        ]
    else:
        start = end = stamp
    raw = RawHealthData(
        google_data_point_id=identifier,
        data_type=kind,
        source_start_time=start,
        source_end_time=end,
        raw_json={"dailyRestingHeartRate": {"date": day.isoformat(), "beatsPerMinute": 71}},
        payload_hash="test",
        first_seen_at=stamp,
        last_seen_at=stamp,
        is_quarantined=quarantined,
        source_deleted_at=stamp if deleted else None,
    )
    return [raw, *normalized]


@pytest.mark.parametrize("metric,expected", [("steps", 600), ("sleep", 27000), ("heart", 71)])
async def test_month_pages_cover_full_archive_and_filter_sources(
    client, sessions, metric, expected
):
    async with sessions() as session:
        session.add_all(
            records(metric, date(2026, 8, 12), "old")
            + records(metric, date(2026, 9, 29), "current")
            + records(metric, date(2020, 1, 1), "quarantined", quarantined=True)
            + records(metric, date(2019, 1, 1), "deleted", deleted=True)
            + records(metric, date(2026, 9, 29), "deleted-recent", deleted=True)
            + records(metric, date(2026, 9, 29), "quarantined-recent", quarantined=True)
            + records(metric, date(2099, 1, 1), "future")
        )
        await session.commit()
    first = (await client.get("/api/dashboard/history", params={"metric": metric})).json()
    assert first["start_date"] == "2026-09-01"
    assert first["end_date"] == "2026-09-30"
    assert first["earliest_date"] == "2026-08-12"
    assert first["latest_date"] == "2026-09-29"
    assert first["total_pages"] == 2
    assert len(first["days"]) == 30
    assert first["days"][0] == {"date": "2026-09-30", "value": None, "partial": True}
    assert first["days"][1]["value"] == expected
    second = (
        await client.get("/api/dashboard/history", params={"metric": metric, "page": 2})
    ).json()
    assert second["start_date"] == "2026-08-02"
    assert second["end_date"] == "2026-08-31"
    assert len(second["days"]) == 30
    assert not any(row["partial"] for row in second["days"])
    assert {row["date"] for row in first["days"]}.isdisjoint(row["date"] for row in second["days"])
    assert next(row for row in second["days"] if row["date"] == "2026-08-12")["value"] == expected
    assert (
        await client.get("/api/dashboard/history", params={"metric": metric, "page": 3})
    ).status_code == 422


@pytest.mark.parametrize("metric", ["steps", "sleep", "heart"])
async def test_empty_archive_has_one_full_empty_page(client, metric):
    response = await client.get("/api/dashboard/history", params={"metric": metric})
    assert response.status_code == 200
    data = response.json()
    assert data["total_pages"] == data["page"] == 1
    assert data["earliest_date"] is data["latest_date"] is None
    assert len(data["days"]) == 30
    assert all(row["value"] is None for row in data["days"])


@pytest.mark.parametrize("metric", ["steps", "sleep"])
async def test_normalized_quarantine_does_not_expand_or_pollute_history(client, sessions, metric):
    async with sessions() as session:
        session.add_all(
            records(metric, date(2026, 9, 29), "current")
            + records(metric, date(2019, 1, 1), "old-bad", normalized_bad=True)
            + records(metric, date(2026, 9, 29), "new-bad", normalized_bad=True)
        )
        await session.commit()
    data = (await client.get("/api/dashboard/history", params={"metric": metric})).json()
    assert data["total_pages"] == 1
    assert data["earliest_date"] == "2026-09-29"
    assert data["days"][1]["value"] == (600 if metric == "steps" else 27000)


async def test_archive_has_no_history_cutoff_and_uses_requested_civil_days(client, sessions):
    async with sessions() as session:
        session.add_all(records("steps", date(2020, 1, 1), "old"))
        await session.commit()
    params = {"metric": "steps", "timezone": "America/Los_Angeles"}
    data = (await client.get("/api/dashboard/history", params=params)).json()
    today = date(2026, 9, 29)  # Still the previous day west of UTC.
    assert data["end_date"] == today.isoformat()
    assert data["total_pages"] == (today - date(2020, 1, 1)).days // 30 + 1
    last = (
        await client.get("/api/dashboard/history", params={**params, "page": data["total_pages"]})
    ).json()
    assert next(row for row in last["days"] if row["date"] == "2020-01-01")["value"] == 600


async def test_daily_heart_date_is_not_shifted_in_western_timezone(client, sessions):
    async with sessions() as session:
        session.add_all(records("heart", date(2026, 9, 29), "daily"))
        await session.commit()
    data = (
        await client.get(
            "/api/dashboard/history", params={"metric": "heart", "timezone": "America/Los_Angeles"}
        )
    ).json()
    assert data["earliest_date"] == data["latest_date"] == "2026-09-29"
    assert data["days"][0]["value"] == 71


@pytest.mark.parametrize(
    "params",
    [
        {"metric": "weight"},
        {"metric": "steps", "page": 0},
        {"metric": "steps", "page": 2},
        {"metric": "steps", "page": "one"},
        {"metric": "steps", "timezone": "wrong"},
        {},
    ],
)
async def test_history_validation(client, params):
    assert (await client.get("/api/dashboard/history", params=params)).status_code == 422
