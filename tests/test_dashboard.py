from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from weight_records import INVALID_FIELDS, correction, weight

from app.db.session import get_session
from app.main import create_app
from app.models.health import SleepSession, Steps, Weight
from app.models.state import RawHealthData
from app.services.dashboard import dashboard_data, sleep_total


def instant(value):
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def raw_daily(kind, payload, identifier="one", quarantined=False):
    end = None
    if kind == "sleep":
        payload = {
            "interval": {
                "startTime": "2026-09-28T22:00:00Z",
                "endTime": "2026-09-29T06:00:00Z",
            },
            "summary": {"minutesAsleep": 450},
        }
        end = instant("2026-09-29T06:00:00")
    key = kind.split("-")[0] + "".join(word.title() for word in kind.split("-")[1:])
    return RawHealthData(
        data_type=kind,
        google_data_point_id=identifier,
        raw_json={key: payload},
        source_start_time=instant("2026-09-29T00:00:00"),
        source_end_time=end,
        payload_hash="test",
        first_seen_at=instant("2026-09-29T01:00:00"),
        last_seen_at=instant("2026-09-29T01:00:00"),
        is_quarantined=quarantined,
    )


def raw_steps(identifier, start, end, count):
    row = raw_daily(
        "steps", {"count": count, "interval": {"startTime": start, "endTime": end}}, identifier
    )
    row.source_start_time, row.source_end_time = instant(start), instant(end)
    return row


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
                # Normalized rows count only with their raw row (app/models/eligibility.py).
                raw_steps("midnight", "2026-09-28T21:30:00", "2026-09-28T22:30:00", 600),
                raw_steps("quarantine", "2026-09-29T12:00:00", "2026-09-29T12:01:00", 999),
                raw_daily("sleep", {}, "sleep"),
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
        session.add(raw_steps("dst", "2025-10-25T22:00:00", "2025-10-26T23:00:00", 2500))
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
        session.add(raw_steps("fractional", "2026-09-28T18:00:00", "2026-09-28T19:00:00", 600))
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
        transport=httpx.ASGITransport(app=application),
        base_url="http://localhost",
        headers={"X-HealthOS-Request": "1"},
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


@pytest.mark.parametrize("fields", INVALID_FIELDS)
@pytest.mark.parametrize("fallback", ["none", "google", "manual", "flagged"])
async def test_weight_corrections_select_current_valid_profile(
    sessions, monkeypatch, fields, fallback
):
    from derived_records import DAY, T0, manual, snapshot, store

    from app.services import codex_health
    from app.services.manual_flags import record_flag

    monkeypatch.setattr(codex_health, "SessionLocal", sessions)
    await store(sessions, "weight", weight())
    if fallback != "none":
        await store(sessions, "weight", weight("other", 68000, T0 - timedelta(days=1)))
    if fallback in {"manual", "flagged"}:
        identifier = await manual(sessions, 69, at=T0 - timedelta(hours=1))
        if fallback == "flagged":
            async with sessions.begin() as session:
                await record_flag(session, identifier, "test", True)
    app = create_app()

    async def session_override():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        for record, expected in (
            (weight(), 70),
            (
                correction(T0 + timedelta(hours=1), fields),
                {"none": None, "google": 68, "manual": 69, "flagged": 68}[fallback],
            ),
            (weight(grams=72000, at=T0 + timedelta(hours=2)), 72),
        ):
            stats = await store(sessions, "weight", record)
            async with sessions() as session:
                if expected not in (70, 72):
                    assert stats.updated == stats.versions_kept == stats.normalization_skipped == 1
                    retained = await session.scalar(
                        select(Weight).where(Weight.google_data_point_id == record["name"])
                    )
                    assert retained.kilograms == 70 and retained.timestamp.replace(tzinfo=UTC) == T0
                before = await snapshot(session)
            response = await client.get("/api/dashboard", params={"end_date": str(DAY)})
            assert response.status_code == 200, response.text
            profile = response.json()["profile"]
            daily = await codex_health.read_health(
                {"action": "daily", "start_date": str(DAY), "end_date": str(DAY)}
            )
            for item in (daily["profile"], profile):
                for measurement in item.values():
                    measurement["measured_at"] = datetime.fromisoformat(measurement["measured_at"])
            assert daily["profile"] == profile
            if expected is None:
                assert "weight" not in profile
            else:
                assert profile["weight"]["value"] == expected
                assert profile["weight"]["source"] == ("Manuel" if expected == 69 else "Google")
                expected_at = {
                    68: T0 - timedelta(days=1),
                    69: T0 - timedelta(hours=1),
                    70: T0,
                    72: T0 + timedelta(hours=2),
                }[expected]
                assert profile["weight"]["measured_at"] == expected_at
            async with sessions() as session:
                assert await snapshot(session) == before


@pytest.mark.parametrize(
    "grams, expected",
    [
        (0, 0),
        ("72500", 72.5),
        ("1e311", 1e308),
        ("-1e311", -1e308),
        ("1e400", None),
        ("1e-400", None),
        ("1e2000000000", None),
        ("NaN", None),
        ("sNaN", None),
        ("Infinity", None),
        ("-Infinity", None),
    ],
)
def test_current_weight_uses_finite_conversion_and_supported_times(grams, expected):
    from app.services.current_weight import current_weight

    record = weight(grams=grams)
    for temporal in (
        {"sampleTime": {"physicalTime": "2026-09-29T00:00:00Z"}},
        {"interval": {"startTime": "2026-09-29T02:00:00+02:00"}},
        {"date": "2026-09-29"},
    ):
        record["weight"] = {"weightGrams": grams, **temporal}
        measurement = current_weight(record)
        assert measurement == (None if expected is None else (instant("2026-09-29"), expected))


@pytest.mark.parametrize("path", ["", "/export.csv"])
@pytest.mark.parametrize(
    "end,days,zone,status",
    [
        ("0001-01-01", 7, "UTC", 422),
        ("0001-01-30", 31, "UTC", 422),
        ("0001-01-01", 1, "Etc/GMT-14", 422),
        ("9999-12-31", 1, "UTC", 422),
        ("0001-01-01", 1, "UTC", 200),
        ("0001-01-31", 31, "UTC", 200),
        ("0001-01-02", 1, "Etc/GMT-14", 200),
        ("9999-12-30", 1, "Etc/GMT+12", 200),
    ],
)
async def test_http_date_windows(dashboard_dates, path, end, days, zone, status):
    client, statements = dashboard_dates
    response = await client.get(
        f"/api/dashboard{path}", params={"end_date": end, "days": days, "timezone": zone}
    )
    assert response.status_code == status, response.text
    if status == 422:
        assert statements == []
        assert response.json() == {
            "detail": "Seçilen tarih aralığı bu saat diliminde hesaplanamıyor."
        }
    elif path:
        assert response.content.startswith(b"\xef\xbb\xbf")
        assert len(response.text.splitlines()) == days + 1
        assert response.headers["cache-control"] == "no-store"
        assert f'filename="healtos-{end}.csv"' in response.headers["content-disposition"]
    else:
        assert response.json()["end_date"] == end
        assert len(response.json()["days"]) == days
        if end == "0001-01-01":
            assert response.json()["days"][0]["steps"] == 240


async def test_latest_weight_reads_past_more_invalid_corrections_than_one_batch(sessions):
    from derived_records import T0, store

    from app.models.eligibility import normalized_counted
    from app.services.current_weight import latest_weight

    # Newer invalid corrections keep their old Weight rows; no total cap may hide
    # the older valid measurement behind several batches of them.
    names = [f"corrected-{i}" for i in range(250)]
    at = [T0 + timedelta(minutes=i) for i in range(250)]
    await store(sessions, "weight", *[weight(n, at=t) for n, t in zip(names, at, strict=True)])
    await store(
        sessions,
        "weight",
        *[weight(n, grams="broken", at=t) for n, t in zip(names, at, strict=True)],
    )
    await store(sessions, "weight", weight("valid", 65000, T0 - timedelta(days=1)))
    async with sessions() as session:
        found = await latest_weight(
            session, T0 + timedelta(days=1), normalized_counted(Weight, ("weight",))
        )
    assert found == (T0 - timedelta(days=1), 65.0)


@pytest.mark.parametrize("window", ["daily", "monthly"])
def test_unrepresentable_window_is_a_domain_error_not_http(window):
    from zoneinfo import ZoneInfo

    from fastapi import HTTPException

    from app.services import dashboard_window

    # The service layer knows nothing of HTTP; the API layer turns this into 422.
    with pytest.raises(dashboard_window.DateWindowError) as raised:
        if window == "daily":
            dashboard_window.daily_window(date.min, 1, ZoneInfo("Etc/GMT-14"))
        else:
            dashboard_window.monthly_window(date.max, ZoneInfo("UTC"))
    assert not isinstance(raised.value, HTTPException)
    assert str(raised.value) == "Seçilen tarih aralığı bu saat diliminde hesaplanamıyor."
