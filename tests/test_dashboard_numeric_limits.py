"""Synthetic archive reads keep finite results and expose overflow as missing data."""

import csv
import io
import json
from datetime import datetime, timedelta
from math import isfinite

import httpx
import pytest
from derived_records import DAY, PHONE, T0, manual, point, sleep, snapshot, steps, streams, summary
from derived_records import store as archive
from fastapi import FastAPI
from numeric_records import store_read_correction as store
from sleep_numeric_records import BAD_SLEEP_NUMBERS, SLEEP_FIELDS, corrected_sleep
from sqlalchemy import select, update

from app.api import dashboard
from app.db.session import get_session
from app.models.health import SleepSession
from app.models.raw_versions import RawHealthDataVersion
from app.models.state import RawHealthData
from app.parsers.sleep import SLEEP_NUMERIC_ERROR
from app.services import codex_health
from app.services.dashboard import dashboard_data


@pytest.fixture
async def api(sessions, monkeypatch):
    class Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return (T0 + timedelta(days=len(SLEEP_FIELDS) + 3)).astimezone(tz)

    monkeypatch.setattr(dashboard, "datetime", Now)
    monkeypatch.setattr(codex_health, "SessionLocal", sessions)
    app = FastAPI()
    app.include_router(dashboard.router)

    async def session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        yield client


async def outputs(api, sessions, key, expected, metric, kind):
    """Exercise the shared calculation through HTTP, CSV and both Umay consumers."""
    last = DAY + timedelta(days=len(expected) - 1)
    query = {"start_date": str(DAY), "end_date": str(last)}
    params = {"end_date": query["end_date"], "days": len(expected)}
    async with sessions() as session:
        before = await snapshot(session)
        data = await dashboard_data(session, last, len(expected), "UTC")
        assert [row[key] for row in reversed(data["days"])] == expected
    response = await api.get("/api/dashboard", params=params)
    assert response.status_code == 200, response.text
    assert [row[key] for row in reversed(response.json()["days"])] == expected
    history = await api.get("/api/dashboard/history", params={"metric": metric})
    assert history.status_code == 200, history.text
    dates = {row["date"]: row["value"] for row in history.json()["days"]}
    assert [dates[str(DAY + timedelta(days=i))] for i in range(len(expected))] == expected
    exported = await api.get("/api/dashboard/export.csv", params=params)
    assert exported.status_code == 200
    rows = list(csv.DictReader(io.StringIO(exported.text.lstrip("\ufeff"))))
    assert [row[key] for row in reversed(rows)] == [
        "" if value is None else str(value) for value in expected
    ]
    assert not any(word in exported.text.lower() for word in ("nan", "inf"))
    daily = await codex_health.read_health({"action": "daily", **query})
    assert [row[key] for row in reversed(daily["days"])] == expected
    card = await codex_health.read_health({"action": "coverage", "data_type": kind, **query})
    [coverage] = card["metrics"]
    assert coverage["value_days"] == sum(value is not None for value in expected)
    assert coverage["days_without_value"] == sum(value is None for value in expected)
    # Reject non-standard JSON constants anywhere, including the derived daily result.
    json.dumps(daily, allow_nan=False)
    json.dumps(card, allow_nan=False)
    async with sessions() as session:
        assert await snapshot(session) == before
    return daily


@pytest.mark.parametrize("value", BAD_SLEEP_NUMBERS)
async def test_sleep_corrections_share_finite_reads_without_reusing_archived_values(
    api, sessions, value
):
    # Every field has its own day and correction chain. Exercise the full matrix
    # without rebuilding the schema and repeating every consumer once per field.
    stats = {}
    for offset, field in enumerate(SLEEP_FIELDS):
        stats[field] = await corrected_sleep(sessions, field, value, T0 + timedelta(days=offset))
    count = len(SLEEP_FIELDS)
    await archive(
        sessions,
        "sleep",
        sleep(
            "zero",
            start=T0 + timedelta(days=count),
            end=T0 + timedelta(days=count, hours=8),
            summary={"minutesAsleep": 0},
        ),
        sleep(
            "sound",
            start=T0 + timedelta(days=count + 1),
            end=T0 + timedelta(days=count + 1, hours=8),
            summary={"minutesAsleep": 420},
        ),
    )
    async with sessions() as session:
        before = await snapshot(session)
        # store_records kept the valid payload as version 1 under the same identity.
        versions = (await session.scalars(select(RawHealthDataVersion))).all()
        assert len(versions) == count
        assert len((await session.scalars(select(SleepSession))).all()) == count + 2
        for old in versions:
            assert old.version == 1 and old.raw_json["sleep"]["summary"]["minutesAsleep"] == 420
            field = old.google_data_point_id.rsplit("/", 1)[-1]
            archived = await session.scalar(
                select(SleepSession).where(
                    SleepSession.google_data_point_id == old.google_data_point_id
                )
            )
            if stats[field].normalization_skipped:
                assert archived.duration_seconds == 28800 and archived.awake_seconds == 1800
    expected = [None] * count + [0.0, 25200.0]
    daily = await outputs(api, sessions, "sleep_seconds", expected, "sleep", "sleep")
    assert all(row["sleep_assumptions"] == [SLEEP_NUMERIC_ERROR] for row in daily["days"][2:])
    assert daily["days"][0]["sleep_assumptions"] == daily["days"][1]["sleep_assumptions"] == []
    for offset in range(count):
        canonical = streams(daily["derived"], "sleep_seconds", day=offset)[0]
        assert canonical["state"] == "no_value" and canonical["value"] is None
        assert SLEEP_NUMERIC_ERROR in canonical["uncertainties"]
        assert canonical["observations"][0]["value"] is None
    result = await codex_health.read_health(
        {
            "action": "sleep",
            "start_date": str(DAY),
            "end_date": str(DAY + timedelta(days=count + 1)),
        }
    )
    assert [row["value"] for row in result["summaries"]] == expected
    json.dumps(result, allow_nan=False)
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize(
    ("counts", "expected"),
    [([1e308], round(1e308)), ([1e308, 1e308, 12], None), ([1e308, 1e308, -1e308], round(1e308))],
)
async def test_step_scale_and_complete_bucket_sum(api, sessions, counts, expected):
    await store(sessions, "steps", *(steps(str(i), count=n) for i, n in enumerate(counts)))
    await store(sessions, "steps", steps("zero", count=0, start=T0 + timedelta(days=1)))
    await store(
        sessions, "steps", steps("sound", count=31, start=T0 + timedelta(days=2), source=PHONE)
    )
    await outputs(api, sessions, "steps", [expected, 0, 31], "steps", "steps")
    response = await api.get("/api/dashboard", params={"end_date": str(DAY), "days": 1})
    assert response.status_code == 200
    assert response.json()["charts"]["steps"] == [
        None if expected is None else float(expected),
        *([None] * 23),
    ]


async def test_step_daily_overflow_keeps_other_hours_and_civil_days(api, sessions):
    await store(
        sessions,
        "steps",
        steps("a", count=1e308),
        steps("b", count=1e308, start=T0 + timedelta(hours=1)),
    )
    await store(sessions, "steps", steps("c", count=7, start=T0 + timedelta(hours=2)))
    await store(
        sessions,
        "steps",
        steps(
            "boundary",
            count=1e308,
            start=T0 + timedelta(days=1, hours=23),
            end=T0 + timedelta(days=2, hours=1),
        ),
    )
    await outputs(api, sessions, "steps", [None, round(5e307), round(5e307)], "steps", "steps")
    async with sessions() as session:
        for day, chart in ((DAY, [1e308, 1e308, 7]), (DAY + timedelta(days=2), [5e307])):
            data = await dashboard_data(session, day, 1, "UTC")
            assert data["charts"]["steps"] == chart + [None] * (24 - len(chart))


async def test_underflowed_step_share_can_belong_to_a_representable_total(api, sessions):
    await store(
        sessions,
        "steps",
        steps(count="5e-324", end=T0 + timedelta(hours=2)),
    )
    await outputs(api, sessions, "steps", [0, None, None], "steps", "steps")
    async with sessions() as session:
        data = await dashboard_data(session, DAY, 1, "UTC")
        assert data["charts"]["steps"] == [None] * 24


async def test_heart_mean_recovers_overflow_and_preserves_rounding(api, sessions):
    for hour, values in (
        (0, [1e308, 1e308]),
        (1, [60.1, 60.2]),
        (2, [0]),
        (3, [1e308, 1e308, -1e308]),
    ):
        await store(
            sessions,
            "heart-rate",
            *(
                point(
                    "heart-rate",
                    f"{hour}-{i}",
                    {
                        "sampleTime": {"physicalTime": (T0 + timedelta(hours=hour)).isoformat()},
                        "beatsPerMinute": value,
                    },
                )
                for i, value in enumerate(values)
            ),
        )
    async with sessions() as session:
        before = await snapshot(session)
    query = {"end_date": str(DAY), "days": 1}
    response = await api.get("/api/dashboard", params=query)
    assert response.status_code == 200, response.text
    expected = [1e308, round((60.1 + 60.2) / 2, 1), 0, 1e308 / 3] + [None] * 20
    assert response.json()["charts"]["heart_rate"] == expected
    daily = await codex_health.read_health(
        {"action": "daily", "start_date": str(DAY), "end_date": str(DAY)}
    )
    assert daily["charts"]["heart_rate"] == expected
    json.dumps(daily, allow_nan=False)
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize("value", ["1e309", "-1e309", "1e-400", "NaN", "Infinity"])
async def test_unrepresentable_daily_payloads_are_missing(api, sessions, value):
    for kind, field in (
        ("daily-resting-heart-rate", "beatsPerMinute"),
        ("daily-heart-rate-variability", "rmssd"),
        ("daily-oxygen-saturation", "averagePercentage"),
    ):
        await store(
            sessions,
            kind,
            summary(kind, **{field: 70}),
            summary(kind, "zero", day=DAY + timedelta(days=1), **{field: 0}),
        )
        # Raw-only fixtures also cover nonfinite payloads that SQLite Numeric cannot store.
        corrected = summary(kind, **{field: value})
        async with sessions() as session, session.begin():
            await session.execute(
                update(RawHealthData)
                .where(RawHealthData.google_data_point_id == corrected["name"])
                .values(raw_json=corrected)
            )
    await outputs(
        api, sessions, "resting_bpm", [None, 0.0, None], "heart", "daily-resting-heart-rate"
    )
    response = await api.get("/api/dashboard", params={"end_date": str(DAY), "days": 1})
    assert all(response.json()["days"][0][key] is None for key in ("hrv_ms", "spo2"))


async def test_profile_conversions_keep_latest_missing_value_and_zero(api, sessions):
    for kind, field in (
        ("height", "heightMillimeters"),
        ("body-fat", "percentage"),
        ("weight", "weightGrams"),
    ):
        for name, at, value in (("old", T0 - timedelta(days=1), 50), ("latest", T0, "1e400")):
            await store(
                sessions,
                kind,
                point(kind, name, {"sampleTime": {"physicalTime": at.isoformat()}, field: value}),
            )
    await manual(sessions, "1e309", metric="muscle")
    async with sessions() as session:
        before = await snapshot(session)
    response = await api.get("/api/dashboard", params={"end_date": str(DAY), "days": 1})
    assert response.status_code == 200, response.text
    assert set(response.json()["profile"]) == {"height", "body_fat", "weight", "muscle"}
    profile = response.json()["profile"]
    assert profile["weight"]["value"] == 0.05  # invalid latest Google weight is not a candidate
    assert all(profile[key]["value"] is None for key in ("height", "body_fat", "muscle"))
    async with sessions() as session:
        assert await snapshot(session) == before
    for metric in ("height", "weight", "body_fat", "muscle"):
        await manual(sessions, 0, at=T0 + timedelta(hours=1), metric=metric)
    response = await api.get("/api/dashboard", params={"end_date": str(DAY), "days": 1})
    assert all(
        item["value"] == 0 and isfinite(item["value"])
        for item in response.json()["profile"].values()
    )


@pytest.mark.parametrize(
    "millimeters, expected",
    [
        ("1e309", 1e308),
        ("-1e309", -1e308),
        ("1e2000000000", None),
        ("1e-2000000000", None),
        ("NaN", None),
        ("Infinity", None),
        ("0e2000000000", 0),
        ("0e-2000000000", 0),
        (1800, 180),
        (0, 0),
    ],
)
async def test_profile_scaling_recovers_representable_source_value(
    api, sessions, millimeters, expected
):
    for value in (1800, millimeters):
        await store(
            sessions,
            "height",
            point(
                "height",
                "one",
                {"sampleTime": {"physicalTime": T0.isoformat()}, "heightMillimeters": value},
            ),
        )
    async with sessions() as session:
        before = await snapshot(session)
    response = await api.get("/api/dashboard", params={"end_date": str(DAY), "days": 1})
    assert response.status_code == 200, response.text
    assert response.json()["profile"]["height"]["value"] == expected
    daily = await codex_health.read_health(
        {"action": "daily", "start_date": str(DAY), "end_date": str(DAY)}
    )
    assert daily["profile"]["height"]["value"] == expected
    json.dumps(daily, allow_nan=False)
    async with sessions() as session:
        assert await snapshot(session) == before
