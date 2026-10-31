"""Final-unit numeric validation through the production reader and raw corrections."""

import json
from copy import deepcopy

import pytest
from derived_records import PHONE, T0, point, snapshot, store
from interpretation_records import call, exercise
from interpretation_records import production_reader as production_reader
from sqlalchemy import select

from app.google_health.data_types import REGISTRY
from app.models.health import Distance, ExerciseSession, ScalarMetric, Weight
from app.models.raw_versions import RawHealthDataVersion
from app.services.interpretation_types import Interpretation

NUMERIC = "numeric_overflow:unit_conversion"
MISSING = "unsupported_or_missing_value"


@pytest.mark.parametrize(
    "kind,field,model,column,factor",
    [
        ("distance", "millimeters", Distance, "meters", 0.001),
        ("weight", "weightGrams", Weight, "kilograms", 0.001),
        ("height", "heightMillimeters", ScalarMetric, "value", 0.1),
        ("exercise", "distanceMillimeters", ExerciseSession, "distance_meters", None),
    ],
)
@pytest.mark.parametrize("corrected", [False, True], ids=["new", "same-id-correction"])
@pytest.mark.parametrize(
    "value,reason",
    [
        *[(v, None) for v in (1234, 1234.56789, "1234.56789", "5e-321", 0, "0", "-0", "0e99999")],
        ("1e309", None),
        (10**309, None),
        *[(v, NUMERIC) for v in (5e-324, "5e-324", "-5e-324", "1e-999999", "1e999999")],
        *[(v, MISSING) for v in ("NaN", "sNaN", "Infinity", "-Infinity")],
        *[(f"1e{sign}99999999999999999999", NUMERIC) for sign in ("", "-")],
        *[(v, MISSING) for v in (None, True, "bad", {}, [], ...)],
    ],
)
async def test_reader_scales_current_raw_values_without_mutating_archive(
    sessions, kind, field, model, column, factor, corrected, value, reason
):
    action = "sessions" if kind == "exercise" else "longitudinal"
    if value is ... and kind != "exercise":
        reason = "unsupported_payload_shape"
    if kind == "exercise":
        seed = exercise("scaled", exerciseType="WALKING")
        seed[REGISTRY[kind].json_key]["metricsSummary"][field] = 72000
    else:
        seed = point(kind, "scaled", {"sampleTime": {"physicalTime": T0.isoformat()}})
        seed[REGISTRY[kind].json_key].update(
            interval={"startTime": T0.isoformat(), "endTime": T0.replace(hour=1).isoformat()},
            **{field: 72000},
        )
    neighbor = deepcopy(seed)
    neighbor.update(name=seed["name"] + "-neighbor", dataSource=PHONE)
    current = deepcopy(seed)
    payload = current[REGISTRY[kind].json_key]
    target = payload["metricsSummary"] if kind == "exercise" else payload
    if value is ...:
        target.pop(field)
    else:
        target[field] = value
    arguments = {} if action == "sessions" else {"data_type": kind}
    if corrected:
        await store(sessions, kind, seed, neighbor)
        cursor = (await call(action, limit=1, **arguments))["next_cursor"]
    written = await store(sessions, kind, current, neighbor)
    async with sessions() as session:
        before = await snapshot(session)
        versions = (await session.scalars(select(RawHealthDataVersion))).all()
        assert len(versions) == int(corrected)
        if corrected:
            assert versions[0].version == 1 and versions[0].raw_json == seed
            # Out-of-range corrections preserve the old normalized row; the reader
            # must still use the current raw value, including its missing result.
            if written.normalization_skipped:
                old = await session.scalar(
                    select(model).where(model.google_data_point_id == seed["name"])
                )
                assert old is not None and float(getattr(old, column)) == (
                    72000 if kind == "height" else 72
                )
        if corrected:
            assert (await call(action, cursor=cursor, **arguments))["code"] == "stale_cursor"
    result = await call(action, **arguments)
    Interpretation.model_validate(result)
    json.dumps(result, allow_nan=False)
    assert result["total_summaries"] == 2
    row = next(r for r in result["summaries"] if r["evidence"][0]["record"] == seed["name"])
    evidence = row["evidence"][0]
    assert evidence["reason_codes"] == ([reason] if reason else [])
    assert (reason in row["reason_codes"]) if reason else NUMERIC not in row["reason_codes"]
    actual = row["distance_meters"] if action == "sessions" else evidence["value"]
    if reason:
        assert actual is None
    elif value in ("1e309", 10**309):
        assert actual == pytest.approx(1e308 if kind == "height" else 1e306)
    else:
        # Keep the historical multiply (measurements) versus divide (sessions)
        # rounding order, rather than merely checking an approximate normal value.
        expected = float(value) / 1000 if factor is None else float(value) * factor
        assert actual == expected
    if action == "sessions":
        assert row["calories_kcal"] == 300 and row["reported_active_seconds"] == 1800
        assert row["exercise_type"] == "WALKING" and row["interval_seconds"] == 5400
        assert row["overlaps"] == [neighbor["name"]]
    else:
        assert row["statistics"]["count"] == int(reason is None)
        assert row["statistics"]["median"] == actual
        assert row["state"] == ("unsupported" if reason else "available")
    filtered = await call(action, source=row["source"], definition=row["definition"], **arguments)
    assert filtered["summaries"] == [row]
    page = await call(action, limit=1, **arguments)
    rest = await call(action, cursor=page["next_cursor"], **arguments)
    assert page["summaries"] + rest["summaries"] == result["summaries"]
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize(
    "value,reason",
    [
        *[(v, MISSING) for v in ("NaN", "sNaN", "Infinity", "-Infinity", "bad", None)],
        *[(f"1e{sign}99999999999999999999", NUMERIC) for sign in ("", "-")],
        *[(v, NUMERIC) for v in ("1e999999", "-1e999999", "1e-999999")],
    ],
)
async def test_unscaled_and_scaled_fields_share_numeric_reasons(sessions, value, reason):
    # body-fat percentage keeps its unit; weight grams scale to kilograms.
    for kind, field in (("body-fat", "percentage"), ("weight", "weightGrams")):
        record = point(kind, kind, {"sampleTime": {"physicalTime": T0.isoformat()}, field: value})
        await store(sessions, kind, record)
    for kind in ("body-fat", "weight"):
        row = (await call("longitudinal", data_type=kind))["summaries"][0]
        assert row["evidence"][0]["reason_codes"] == [reason], kind
        assert row["evidence"][0]["value"] is None and reason in row["reason_codes"]
