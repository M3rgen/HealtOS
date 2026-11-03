"""Both coverage readers reject invalid numbers without repairing archived records.

A parsed NaN/Infinity cannot reach the archive: jsonb cannot hold it, so the write path sets it
apart (raw_health_data_rejections) and coverage sees only what is archived.
"""

import math

import pytest
from derived_records import BAND, PHONE, point, snapshot, store
from retrieval_records import START
from retrieval_records import point as response_point
from sqlalchemy import select
from test_coverage import archive as archive
from test_coverage import daily, query
from test_retrieval_coverage_api import PATH, QUERY, fetch
from test_retrieval_coverage_api import api as api

from app.google_health.data_types import REGISTRY
from app.models.raw_rejections import RawHealthDataRejection
from app.models.raw_versions import RawHealthDataVersion
from app.models.state import RawHealthData
from app.services.codex_health import read_health

KINDS = [
    ("daily-resting-heart-rate", "beatsPerMinute", {}),
    ("daily-heart-rate-variability", "averageHeartRateVariabilityMilliseconds", {"rmssd": 40}),
    ("daily-oxygen-saturation", "averagePercentage", {"percentage": 97, "average": 98}),
    ("height", "heightMillimeters", {}),
    ("body-fat", "percentage", {}),
]


def measurement(kind, field, value, fallback, name="one", source=BAND):
    return point(
        kind,
        name,
        {
            "date": START.date().isoformat(),
            "sampleTime": {"physicalTime": START.isoformat()},
            **fallback,
            field: value,
        },
        source,
    )


async def reports(api, kind):
    card = await read_health(query(START.date(), START.date(), data_type=kind))
    response = await api.get(PATH, params=QUERY | {"data_type": kind})
    assert response.status_code == 200, response.text
    return card["metrics"][0], response.json()["types"][0]


def assert_values(card, report, count, invalid):
    assert card["value_days"] == int(count > 0)
    assert card["days_without_value"] == int(count == 0)
    assert card["sampling"]["sample_count"] == count
    assert report["days"][0]["has_value"] is (count > 0)
    for result in (card, report):
        assert result["exclusions"]["invalid_shape"] == invalid
        assert sum(row["record_count"] for row in result["source_regimes"]) == count
        assert all(row["value_days"] == 1 for row in result["source_regimes"])


@pytest.mark.parametrize("kind,field,fallback", KINDS)
@pytest.mark.parametrize("corrected", [False, True], ids=["synthetic-first", "correction"])
@pytest.mark.parametrize(
    "value",
    [
        "NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        "1e310",
        "1e-999999",
        "-1e-999999",
        "1e999999",
        "-1e999999",
        "5e-324",
        "1e309",
        10**309,
        float("nan"),
        float("inf"),
        float("-inf"),
        0,
        "-0",
        "0e99999",
        "1234.56789",
        "5e-321",
        "1e308",
    ],
)
async def test_final_unit_presence_preserves_archive(
    archive, api, sessions, kind, field, fallback, corrected, value
):
    seed = measurement(kind, field, 1234, fallback)
    current = measurement(kind, field, value, fallback)
    if corrected:
        await store(sessions, kind, seed)
    else:
        current.pop("name")
    stats = await store(sessions, kind, current)
    rejected = isinstance(value, float) and not math.isfinite(value)
    assert stats.rejected == int(rejected)
    present = value in (0, "-0", "0e99999", "1234.56789", "5e-321", "1e308") or (
        value in ("1e309", 10**309) if kind == "height" else value == "5e-324"
    )
    async with sessions() as session:
        before = await snapshot(session)
        versions = (await session.scalars(select(RawHealthDataVersion))).all()
        if rejected:
            # Set apart, not written: a correction leaves the stored seed as it was.
            assert len(versions) == 0
            assert (await session.scalars(select(RawHealthDataRejection))).one().ordinal == 0
            raw = (await session.scalars(select(RawHealthData))).all()
            assert [row.raw_json for row in raw] == ([seed] if corrected else [])
            present = corrected
        else:
            assert len(versions) == int(corrected)
            if corrected:
                assert versions[0].raw_json == seed
    card, report = await reports(api, kind)
    assert_values(card, report, int(present), int(not present and not rejected))
    assert card["retrieval"]["state"] == report["days"][0]["retrieval"] == "unknown"
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize("kind,field,fallback", KINDS)
@pytest.mark.parametrize("source", [BAND, PHONE], ids=["same-source", "other-source"])
async def test_invalid_correction_keeps_other_valid_record_on_same_day(
    archive, api, sessions, kind, field, fallback, source
):
    seed = measurement(kind, field, 1234, fallback)
    neighbor = measurement(kind, field, "0", fallback, "neighbor", source)
    await store(sessions, kind, seed, neighbor)
    current = measurement(kind, field, "NaN", fallback)
    await store(sessions, kind, current)
    async with sessions() as session:
        before = await snapshot(session)
    card, report = await reports(api, kind)
    assert_values(card, report, 1, 1)
    assert card["source_regimes"] == report["source_regimes"]
    assert card["source_regimes"][0]["platform"] == source["platform"]
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize("kind,field,fallback", KINDS)
@pytest.mark.parametrize(
    "flag,exclusion", [("is_quarantined", "quarantined"), ("source_deleted_at", "source_deleted")]
)
async def test_excluded_invalid_values_are_not_counted_twice(
    archive, api, sessions, kind, field, fallback, flag, exclusion
):
    await store(sessions, kind, measurement(kind, field, "NaN", fallback))
    async with sessions.begin() as session:
        row = await session.scalar(select(RawHealthData))
        setattr(row, flag, True if flag == "is_quarantined" else START)
    async with sessions() as session:
        before = await snapshot(session)
    card, report = await reports(api, kind)
    assert_values(card, report, 0, 0)
    assert card["exclusions"][exclusion] == report["exclusions"][exclusion] == 1
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize(
    "kind,preferred,fallback",
    [
        (
            "daily-heart-rate-variability",
            "averageHeartRateVariabilityMilliseconds",
            "rootMeanSquareOfSuccessiveDifferencesMilliseconds",
        ),
        (
            "daily-heart-rate-variability",
            "rootMeanSquareOfSuccessiveDifferencesMilliseconds",
            "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds",
        ),
        (
            "daily-heart-rate-variability",
            "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds",
            "rmssd",
        ),
        ("daily-oxygen-saturation", "averagePercentage", "percentage"),
        ("daily-oxygen-saturation", "percentage", "average"),
    ],
)
@pytest.mark.parametrize("value,present", [("sNaN", False), ("not numeric", True)])
async def test_raw_daily_field_selection_precedes_validation(
    archive, kind, preferred, fallback, value, present
):
    day = START.date()
    await archive(daily(kind, "priority", day, {preferred: value, fallback: "0"}))
    metric = (await read_health(query(day, day, data_type=kind)))["metrics"][0]
    assert metric["value_days"] == metric["sampling"]["sample_count"] == int(present)
    assert metric["exclusions"]["invalid_shape"] == int(not present)


@pytest.mark.parametrize(
    "kind,field",
    [
        ("daily-resting-heart-rate", "beatsPerMinute"),
        ("daily-heart-rate-variability", "averageHeartRateVariabilityMilliseconds"),
        ("daily-oxygen-saturation", "averagePercentage"),
        ("height", "heightMillimeters"),
        ("body-fat", "percentage"),
    ],
)
@pytest.mark.parametrize("corrected", [False, True])
async def test_invalid_numeric_return_keeps_completed_with_records(
    api, sessions, kind, field, corrected
):
    record = response_point(kind=kind, date=START.date().isoformat(), **{field: 1234})
    if corrected:
        await fetch(sessions, kind=kind, points=[record])
    record[REGISTRY[kind].json_key][field] = "NaN"
    await fetch(sessions, kind=kind, points=[record])
    async with sessions() as session:
        before = await snapshot(session)
    response = await api.get(PATH, params=QUERY | {"data_type": kind})
    assert response.status_code == 200, response.text
    report = response.json()["types"][0]
    result = report["days"][0]
    assert result["has_value"] is False
    assert result["retrieval"] == "completed_with_records"
    assert result["returned_records"] == 1 + int(corrected)
    assert report["exclusions"]["invalid_shape"] == 1
    assert report["source_regimes"] == []
    async with sessions() as session:
        assert await snapshot(session) == before
