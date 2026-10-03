from datetime import UTC, date, datetime

import httpx
from sqlalchemy import select

from app.db.session import get_session
from app.main import create_app
from app.models.health import SleepSession, Steps
from app.models.state import RawHealthData
from app.services.dashboard import dashboard_data, sleep_total


def instant(value):
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def raw_daily(kind, payload, identifier="one", quarantined=False):
    key = kind.split("-")[0] + "".join(word.title() for word in kind.split("-")[1:])
    return RawHealthData(
        data_type=kind,
        google_data_point_id=identifier,
        raw_json={key: payload},
        source_start_time=instant("2026-09-29T00:00:00"),
        payload_hash="test",
        first_seen_at=instant("2026-09-29T01:00:00"),
        last_seen_at=instant("2026-09-29T01:00:00"),
        is_quarantined=quarantined,
    )


async def test_dashboard_civil_days_daily_sources_and_quarantine(sessions):
    async with sessions() as session:
        session.add_all(
            [
                Steps(
                    google_data_point_id="midnight",
                    start_time=instant("2026-09-28T21:30:00"),
                    end_time=instant("2026-09-28T22:30:00"),
                    count=600,
                ),
                Steps(
                    google_data_point_id="quarantine",
                    start_time=instant("2026-09-29T12:00:00"),
                    end_time=instant("2026-09-29T12:01:00"),
                    count=999,
                    is_quarantined=True,
                ),
                SleepSession(
                    google_data_point_id="sleep",
                    start_time=instant("2026-09-28T22:00:00"),
                    end_time=instant("2026-09-29T06:00:00"),
                    duration_seconds=28800,
                    awake_seconds=1800,
                ),
                raw_daily(
                    "daily-heart-rate-variability",
                    {"date": "2026-09-29", "averageHeartRateVariabilityMilliseconds": 32},
                ),
                raw_daily("heart-rate-variability", {"date": "2026-09-29", "rmssd": 99}),
                raw_daily(
                    "daily-oxygen-saturation",
                    {"date": {"year": 2026, "month": 9, "day": 29}, "averagePercentage": 97.4},
                ),
                raw_daily("daily-resting-heart-rate", {"date": "2026-09-29", "beatsPerMinute": 71}),
            ]
        )
        await session.commit()
        data = await dashboard_data(session, date(2026, 9, 29), 3, "Europe/Paris")
    assert [row["steps"] for row in data["days"]] == [300, 300, None]
    assert data["charts"]["steps"][0] == 300
    assert data["days"][0]["sleep_seconds"] == 27000
    assert data["days"][0]["hrv_ms"] == 32  # Intraday samples are not mixed in.
    assert data["days"][0]["spo2"] == 97.4
    assert data["days"][0]["resting_bpm"] == 71
    assert data["days"][0]["status"] == "complete"
    assert data["days"][2]["status"] == "empty"


def test_overlapping_sleep_is_counted_once():
    def session(start, end):
        return SleepSession(start_time=instant(start), end_time=instant(end), awake_seconds=0)

    rows = [
        session("2026-09-28T22:00:00", "2026-09-29T06:00:00"),
        session("2026-09-28T23:00:00", "2026-09-29T05:00:00"),
    ]
    assert sleep_total(rows) == 8 * 3600
    assert sleep_total([]) is None


async def test_dst_fallback_day_contains_both_repeated_hours(sessions):
    async with sessions() as session:
        session.add(
            Steps(
                google_data_point_id="dst",
                count=2500,
                start_time=instant("2025-10-25T22:00:00"),
                end_time=instant("2025-10-26T23:00:00"),
            )
        )
        await session.commit()
        data = await dashboard_data(session, date(2025, 10, 26), 1, "Europe/Paris")
    assert data["days"][0]["steps"] == 2500
    assert data["charts"]["steps"][2] == 200
    assert sum(data["charts"]["steps"]) == 2500


async def test_fractional_offset_midnight_is_split_at_local_boundary(sessions):
    async with sessions() as session:
        session.add(
            Steps(
                google_data_point_id="fractional",
                count=600,
                start_time=instant("2026-09-28T18:00:00"),
                end_time=instant("2026-09-28T19:00:00"),
            )
        )
        await session.commit()
        data = await dashboard_data(session, date(2026, 9, 29), 2, "Asia/Kolkata")
    assert [row["steps"] for row in data["days"]] == [300, 300]
    assert data["charts"]["steps"][0] == 300


async def test_manual_api_validation_persistence_csv_and_empty_archive(sessions):
    application = create_app()

    async def override_session():
        async with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://localhost"
    ) as client:
        response = await client.get("/api/dashboard?end_date=2026-09-29")
        assert response.status_code == 200
        assert len(response.json()["days"]) == 7
        assert response.json()["days"][0]["steps"] is None
        body = {"metric": "muscle", "value": "42.5", "measured_at": "2026-09-29T09:00:00+02:00"}
        response = await client.post("/api/dashboard/measurements", json=body)
        assert response.status_code == 201
        assert response.json()["source"] == "Manuel"
        for invalid in (
            {**body, "value": "101"},
            {**body, "value": "NaN"},
            {**body, "measured_at": "2026-09-29T09:00:00"},
            {**body, "measured_at": "2099-01-01T09:00:00Z"},
        ):
            assert (
                await client.post("/api/dashboard/measurements", json=invalid)
            ).status_code == 422
        assert len((await client.get("/api/dashboard/measurements")).json()) == 1
        response = await client.get("/api/dashboard?end_date=2026-09-29")
        assert response.json()["profile"]["muscle"]["value"] == 42.5
        csv = await client.get("/api/dashboard/export.csv?end_date=2026-09-29")
        assert csv.status_code == 200
        assert "attachment" in csv.headers["content-disposition"]
        assert "2026-09-29,,,,,,empty,UTC" in csv.text
        assert (await client.get("/api/dashboard?timezone=wrong")).status_code == 422
        assert (await client.get("/api/dashboard?days=10000")).status_code == 422
        assert (await client.get("/api/dashboard?end_date=2099-01-01")).status_code == 422
    await application.state.runtime.shutdown()
    async with sessions() as session:
        assert list(await session.scalars(select(RawHealthData))) == []
