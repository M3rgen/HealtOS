"""Interpretation contracts through the real reader and SQLite archive."""

import json
from datetime import timedelta

import pytest
from derived_records import (
    BAND,
    DAY,
    PHONE,
    T0,
    manual,
    point,
    sleep,
    snapshot,
    stage,
    steps,
    store,
    summary,
)
from interpretation_records import PAGE_ERROR, QUERY, call, exercise, resting
from interpretation_records import production_reader as production_reader
from sqlalchemy import select, update

from app.models.health import ExerciseSession, RestingHeartRate
from app.models.identity import RawSourceDevice
from app.models.manual import ManualWorkout
from app.models.state import RawHealthData
from app.parsers.common import payload_hash
from app.services import health_interpretation, interpretation_inputs
from app.services.codex_health import HEALTH_TOOL, ArchiveQuery, read_health
from app.services.interpretation_types import ACTIONS, Interpretation
from app.services.manual_flags import record_flag


@pytest.mark.parametrize("action", sorted(ACTIONS))
async def test_new_actions_are_advertised_and_return_typed_complete_json(action):
    arguments = dict(QUERY, action=action)
    if action == "relationship":
        arguments.update(metric="resting_bpm", other_metric="hrv_ms", lags=[0, 1])
    assert action in HEALTH_TOOL["inputSchema"]["properties"]["action"]["enum"]
    result = await read_health(arguments)
    assert result["schema_version"] == "2" and "error" not in result
    assert Interpretation.model_validate(result).action == action
    json.dumps(result, allow_nan=False)


async def test_past_only_reference_zero_mad_and_measured_zero(sessions):
    await resting(sessions, [50] * 28, start=DAY - timedelta(days=28))
    await resting(sessions, [0, 999999])
    result = await call("baseline", metric="resting_bpm")
    baseline = result["summaries"][0]
    assert baseline["eligible"]
    assert baseline["statistics"] == dict(
        count=28, median=50, mad=0, minimum=50, maximum=50, p10=50, p90=50
    )
    assert baseline["value"] == 0 and baseline["percentile"] == 0
    assert baseline["robust_z"] is None and "zero_mad" in baseline["reason_codes"]
    assert max(baseline["comparison_dates"]) < str(DAY)
    assert all(e["date"] <= str(DAY) for e in baseline["evidence"])
    assert result == await call("baseline", metric="resting_bpm", limit=1)
    analyzed = await call("analyze", metric="resting_bpm")
    candidate = analyzed["candidates"][0]
    assert candidate["rank"] == 1 and candidate["summary_index"] == 0
    assert candidate["evidence_dates"] == [str(DAY), *baseline["comparison_dates"]]


@pytest.mark.parametrize(
    "count,reason",
    [
        (0, "cold_start"),
        (6, "cold_start"),
        (10, "insufficient_baseline_days"),
        (14, "insufficient_calendar_support"),
    ],
)
async def test_cold_start_and_sparse_calendar_do_not_generate_candidates(sessions, count, reason):
    await resting(sessions, [50] * count, start=DAY - timedelta(days=count))
    await resting(sessions, [90])
    result = await call("analyze", metric="resting_bpm")
    row = result["summaries"][0]
    assert not row["eligible"] and reason in row["reason_codes"]
    assert row["statistics"]["count"] == count and row["percentile"] is None
    assert not result["candidates"]


async def test_method_break_and_unknown_sources_never_merge(sessions):
    await resting(sessions, [50] * 30, start=DAY - timedelta(days=30), method="A")
    await resting(sessions, [60], method="B")
    result = await call("baseline", metric="resting_bpm")
    assert result["summaries"][0]["support_days"] == 0
    assert "definition_or_method_break" in result["summaries"][0]["reason_codes"]
    await store(
        sessions,
        "daily-oxygen-saturation",
        summary("daily-oxygen-saturation", "a", source=None, averagePercentage=95),
        summary("daily-oxygen-saturation", "b", source=None, averagePercentage=96),
    )
    spo2 = await call("baseline", metric="spo2")
    assert len(spo2["summaries"]) == 2 and all(r["support_days"] == 0 for r in spo2["summaries"])


async def test_rename_continuity_requires_existing_identity_decision(sessions):
    renamed = {**BAND, "device": {"displayName": "Renamed synthetic band"}}
    await resting(sessions, [50] * 28, start=DAY - timedelta(days=28))
    await resting(sessions, [60], source=renamed)
    assert (await call("baseline", metric="resting_bpm"))["summaries"][0]["support_days"] == 0
    async with sessions() as session, session.begin():
        for metadata in (BAND, renamed):
            session.add(
                RawSourceDevice(
                    data_type="daily-resting-heart-rate",
                    fingerprint=payload_hash(metadata),
                    device="d" * 64,
                    decided_at=T0,
                )
            )
    proven = (await call("baseline", metric="resting_bpm"))["summaries"][0]
    assert proven["support_days"] == 28 and proven["eligible"]
    await resting(sessions, [70], source=PHONE, start=DAY - timedelta(days=1))
    assert (await call("baseline", metric="resting_bpm"))["summaries"][0]["support_days"] == 28


async def test_sleep_preserves_split_unknown_overlap_and_unsupported_timeline(sessions):
    await store(
        sessions,
        "sleep",
        sleep("a", stages=[stage("LIGHT", 1, 2)]),
        sleep(
            "b",
            start=T0 + timedelta(hours=2),
            end=T0 + timedelta(hours=4),
            summary={"minutesAsleep": 60},
        ),
        sleep(
            "c",
            start=T0 + timedelta(hours=6),
            end=T0 + timedelta(hours=7),
            summary={"minutesAsleep": 30},
        ),
    )
    result = (await call("sleep"))["summaries"][0]
    assert len(result["sleep"]["episodes"]) == 2
    assert result["sleep"]["unknown_seconds"] > 0
    assert result["sleep"]["session_overlap_seconds"] == 3600
    assert result["sleep"]["between_sessions_seconds"] == 7200
    assert "awake_unknown" in result["reason_codes"]
    assert result["sleep"]["sessions"][1]["timeline"] is None
    await store(sessions, "sleep", sleep("a", summary={"minutesAsleep": 45}))
    result = (await call("sleep"))["summaries"][0]
    assert result["sleep"]["timeline"] is None
    assert "timeline_unavailable" in result["reason_codes"]


@pytest.mark.parametrize("day,hours", [("2026-03-29", 23), ("2026-10-25", 25)])
async def test_intraday_real_dst_duration_and_coarse_steps_never_become_bursts(
    sessions, day, hours
):
    from datetime import date

    from app.services.canonical_daily import bounds

    left, right = bounds(date.fromisoformat(day), "Europe/Paris")
    await store(sessions, "steps", steps(start=left, end=right, count=hours * 60))
    result = await call("intraday", start_date=day, end_date=day, timezone="Europe/Paris")
    row = result["summaries"][0]
    assert row["day_seconds"] == row["supported_seconds"] == hours * 3600
    assert row["gap_seconds"] == row["conflict_seconds"] == 0
    assert row["time_weighted_mean"] == pytest.approx(1 / 60)
    assert row["exact_bursts"] is None and len(row["cells"]) == 1


async def test_heart_distribution_uses_elapsed_seconds_and_leaves_gaps(sessions):
    await store(
        sessions,
        "heart-rate",
        *[
            point(
                "heart-rate",
                str(index),
                {
                    "sampleTime": {"physicalTime": (T0 + timedelta(seconds=offset)).isoformat()},
                    "beatsPerMinute": value,
                },
            )
            for index, (offset, value) in enumerate([(60, 60), (120, 120), (3600, 80), (3660, 80)])
        ],
    )
    row = (await call("intraday", data_type="heart-rate"))["summaries"][0]
    assert row["supported_seconds"] == 120 and row["gap_seconds"] == 86400 - 120
    assert row["time_weighted_mean"] == 85
    assert row["statistics"]["count"] == 4
    assert row["cells"][0]["state"] == row["cells"][-1]["state"] == "gap"


@pytest.mark.parametrize("field", ["exerciseType", "displayName"])
@pytest.mark.parametrize(
    "value,primary,secondary",
    [
        *[(v, None, None) for v in ({"bad": 1}, ["bad"], 42, True, "ç" * 129)],
        *[(v, "Walk", None) for v in (None, {}, [], 0, False)],
        ("", "Walk", ""),
        ("RUN_WALK", "RUN_WALK", "RUN_WALK"),
        *[(v, v, v) for v in (" " + "ç" * 126 + " ", "ç" * 128)],
    ],
)
async def test_session_label_correction_preserves_evidence_and_archive(
    sessions, field, value, primary, secondary
):
    fields = {"displayName": "Walk", field: value}
    expected = primary if field == "exerciseType" else secondary
    original = exercise("corrected", exerciseType="RUN_WALK")
    await store(sessions, "exercise", original, exercise("neighbor", exerciseType="WALKING"))
    async with sessions.begin() as session:
        session.add(
            ManualWorkout(exercise_type="run", start_time=T0, end_time=T0 + timedelta(hours=2))
        )
    initial = (await call("sessions"))["summaries"]
    original_row = next(r for r in initial if r["evidence"][0]["record"] == original["name"])
    assert original_row["start"] == (T0 - timedelta(minutes=30)).isoformat()
    assert (
        original_row["interval_seconds"] == 5400 and original_row["reported_active_seconds"] == 1800
    )
    assert original_row["distance_meters"] == 4000 and original_row["calories_kcal"] == 300
    assert all(len(r["overlaps"]) == 2 and r["continuous_running_seconds"] is None for r in initial)
    cursor = (await call("sessions", limit=1))["next_cursor"]
    for labels, label in [(fields, expected), ({}, None), ({"exerciseType": "CYCLING"}, "CYCLING")]:
        await store(sessions, "exercise", exercise("corrected", **labels))
        async with sessions() as session:
            before = await snapshot(session)
        result = await call("sessions")
        Interpretation.model_validate(result)
        json.dumps(result, allow_nan=False)
        assert result["summaries"] == [
            {**r, "exercise_type": label} if r is original_row else r for r in initial
        ]
        assert (await call("sessions", source=original_row["source"]))["total_summaries"] == 2
        assert (await call("sessions", definition="manual_workout_session"))["summaries"] == [
            result["summaries"][-1]
        ]
        page = await call("sessions", limit=1)
        rest = await call("sessions", cursor=page["next_cursor"])
        assert page["summaries"] + rest["summaries"] == result["summaries"]
        assert (await call("sessions", cursor=cursor))["code"] == "stale_cursor"
        cursor = page["next_cursor"]
        async with sessions() as session:
            assert await snapshot(session) == before


async def test_typed_physiology_definitions_units_method_age_and_absence(sessions):
    first = DAY - timedelta(days=40)
    await store(sessions, "daily-vo2-max", summary("daily-vo2-max", day=first, vo2Max=42))
    await store(
        sessions,
        "daily-sleep-temperature-derivations",
        summary(
            "daily-sleep-temperature-derivations",
            nightlyTemperatureCelsius=36,
            baselineTemperatureCelsius=35,
            measurementMethod="skin",
        ),
    )
    await store(
        sessions,
        "daily-respiratory-rate",
        summary(
            "daily-respiratory-rate", breathsPerMinute=0, fullSleepStats={"breathsPerMinute": 12}
        ),
    )
    result = await call("physiology", start_date=str(first))
    rows = result["summaries"]
    vo2 = next(r for r in rows if r["metric"] == "daily-vo2-max")
    assert vo2["unit"] == "ml/kg/min" and vo2["freshness"] == "stale" and vo2["age_days"] == 40
    temperatures = [r for r in rows if r["metric"] == "daily-sleep-temperature-derivations"]
    assert len(temperatures) == 2 and len({r["definition"] for r in temperatures}) == 2
    respiration = [r for r in rows if r["metric"] == "daily-respiratory-rate"]
    assert sorted(r["statistics"]["median"] for r in respiration) == [0, 12]
    assert next(r for r in rows if r["metric"] == "core-body-temperature")["state"] == "absent"
    await store(
        sessions,
        "core-body-temperature",
        point(
            "core-body-temperature",
            "bad",
            {"sampleTime": {"physicalTime": T0.isoformat()}, "newUnknownField": 99},
        ),
    )
    row = (await call("physiology", data_type="core-body-temperature"))["summaries"][0]
    assert row["state"] == "unsupported" and "unsupported_payload_shape" in row["reason_codes"]


async def test_longitudinal_keeps_active_total_and_manual_flag_exclusions(sessions):
    for kind, value in [
        ("active-energy-burned", 100),
        ("total-calories", 2000),
        ("distance", 1000),
    ]:
        await store(
            sessions,
            kind,
            point(
                kind,
                "one",
                {
                    "interval": {
                        "startTime": T0.isoformat(),
                        "endTime": (T0 + timedelta(hours=1)).isoformat(),
                    },
                    "millimeters" if kind == "distance" else "kcal": value,
                },
            ),
        )
    await manual(sessions, 70, at=T0 - timedelta(days=1))
    flagged = await manual(sessions, 999)
    await manual(sessions, 72, at=T0 + timedelta(hours=1))
    async with sessions() as session, session.begin():
        await record_flag(session, flagged, "test", True)
    result = await call("longitudinal", start_date=str(DAY - timedelta(days=1)))
    rows = result["summaries"]
    assert {r["metric"] for r in rows if r["state"] == "available"} == {
        "active-energy-burned",
        "total-calories",
        "distance",
        "weight",
    }
    weight = next(r for r in rows if r["metric"] == "weight" and r["source"] == "manuel")
    assert weight["change"] == 2 and weight["statistics"]["count"] == 2
    assert all(e["record"] != str(flagged) for r in rows for e in r["evidence"])
    assert all(e["value"] != 999 for r in rows for e in r["evidence"])


async def test_statistics_independent_of_page_and_value_correction_expires_cursor(sessions):
    await resting(sessions, [50, 51, 52])
    result = await call("baseline", end_date=str(DAY + timedelta(days=2)), limit=1)
    assert result["next_cursor"]
    large = await call("baseline", end_date=str(DAY + timedelta(days=2)), limit=200)
    second = await call(
        "baseline", end_date=str(DAY + timedelta(days=2)), limit=2, cursor=result["next_cursor"]
    )
    assert result["summaries"] + second["summaries"] == large["summaries"]
    await resting(sessions, [70])
    stale = await call(
        "baseline", end_date=str(DAY + timedelta(days=2)), cursor=result["next_cursor"]
    )
    assert stale["code"] == "stale_cursor"


@pytest.mark.parametrize(
    "mutation", ["source_deleted", "raw_quarantine", "normalized_quarantine", "identity"]
)
@pytest.mark.parametrize("action", ["baseline", "sessions"])
async def test_eligibility_and_identity_changes_expire_cursors(sessions, mutation, action):
    if action == "baseline":
        await resting(sessions, [50, 51, 52])
    else:
        await store(sessions, "exercise", *[exercise(str(i), exerciseType=True) for i in range(3)])
    args = dict(end_date=str(DAY + timedelta(days=2)), limit=1)
    result = await call(action, **args)
    async with sessions() as session, session.begin():
        row = (
            await session.scalars(select(RawHealthData).order_by(RawHealthData.source_start_time))
        ).first()
        if mutation == "source_deleted":
            await session.execute(
                update(RawHealthData).where(RawHealthData.id == row.id).values(source_deleted_at=T0)
            )
        elif mutation == "raw_quarantine":
            await session.execute(
                update(RawHealthData).where(RawHealthData.id == row.id).values(is_quarantined=True)
            )
        elif mutation == "normalized_quarantine":
            model = RestingHeartRate if action == "baseline" else ExerciseSession
            await session.execute(
                update(model)
                .where(model.google_data_point_id == row.google_data_point_id)
                .values(is_quarantined=True)
            )
        else:
            session.add(
                RawSourceDevice(
                    data_type=row.data_type,
                    fingerprint=payload_hash(BAND),
                    device="e" * 64,
                    decided_at=T0,
                )
            )
    assert (await call(action, **args, cursor=result["next_cursor"]))["code"] == "stale_cursor"
    if action == "sessions":
        current = await call(action)
        assert current["total_summaries"] == (3 if mutation == "identity" else 2)
        if mutation == "identity":
            assert current["summaries"][0]["source"] != result["summaries"][0]["source"]
        else:
            assert row.google_data_point_id not in {
                item["evidence"][0]["record"] for item in current["summaries"]
            }


async def test_manual_mark_and_unmark_expire_longitudinal_cursor(sessions):
    marked = await manual(sessions, 70, metric="body_fat")
    before = await call("longitudinal", limit=1)
    assert before["next_cursor"] and before["total_summaries"] == 6
    for flagged in (True, False):
        async with sessions.begin() as session:
            await record_flag(session, marked, "test", flagged)
        assert (await call("longitudinal", limit=1, cursor=before["next_cursor"]))[
            "code"
        ] == "stale_cursor"


@pytest.mark.parametrize(
    "arguments,code",
    [
        ({"start_date": "2025-01-01"}, "invalid_query"),
        ({"timezone": "missing/timezone"}, "invalid_query"),
        ({"cursor": "not-json"}, "invalid_cursor"),
        ({"data_type": "invented"}, "unsupported_data_type"),
    ],
)
async def test_structured_refusals_are_json(arguments, code):
    result = await call("longitudinal", **arguments)
    assert result["schema_version"] == "2" and result["code"] == code
    assert json.loads(json.dumps(result))["error"]


async def test_query_bound_cursor_and_response_and_input_budgets(sessions, monkeypatch):
    before = await call("longitudinal", limit=1)
    result = await call("physiology", cursor=before["next_cursor"])
    assert result["code"] == "cursor_query_mismatch"
    monkeypatch.setattr(health_interpretation, "RESPONSE_BUDGET_BYTES", 1)
    refused = await call("longitudinal")
    assert refused["code"] == "response_budget_exceeded" and refused["error"] == PAGE_ERROR
    await resting(sessions, [50])
    monkeypatch.setattr(interpretation_inputs, "MAX_INPUT_RECORDS", 0)
    assert (await call("baseline"))["code"] == "input_budget_exceeded"


def test_query_rejects_unplanned_duplicate_or_excessive_lags():
    for lags in ([15], [1, 1], list(range(6))):
        with pytest.raises(ValueError):
            ArchiveQuery.model_validate(
                dict(
                    action="relationship", **QUERY, metric="steps", other_metric="hrv_ms", lags=lags
                )
            )


async def test_uncertain_sleep_and_partial_steps_are_not_comparable_references(sessions):
    await store(sessions, "sleep", sleep(stages=[stage("LIGHT", 1, 2)]))
    await store(sessions, "steps", steps(count=0))
    result = await call("analyze")
    rows = {r["metric"]: r for r in result["summaries"]}
    assert "awake_unknown" in rows["sleep_seconds"]["reason_codes"]
    assert "incomplete_interval_coverage" in rows["steps"]["reason_codes"]
    assert all(not r["eligible"] for r in rows.values())
    assert not result["candidates"]


async def test_today_is_partial_even_when_daily_summary_exists(sessions):
    from datetime import UTC, datetime

    today = datetime.now(UTC).date()
    await resting(sessions, [60], start=today)
    row = (await call("baseline", start_date=str(today), end_date=str(today)))["summaries"][0]
    assert "partial_day" in row["reason_codes"] and not row["eligible"]


async def test_source_recording_method_breaks_comparison_despite_device_membership(sessions):
    changed = {**BAND, "recordingMethod": "MANUAL"}
    await resting(sessions, [50] * 28, start=DAY - timedelta(days=28))
    await resting(sessions, [60], source=changed)
    async with sessions() as session, session.begin():
        for metadata in (BAND, changed):
            session.add(
                RawSourceDevice(
                    data_type="daily-resting-heart-rate",
                    fingerprint=payload_hash(metadata),
                    device="d" * 64,
                    decided_at=T0,
                )
            )
    row = (await call("baseline", metric="resting_bpm"))["summaries"][0]
    assert row["support_days"] == 0 and not row["eligible"]
    assert row["method_details"]["recordingMethod"] == "MANUAL"


async def test_repeated_timestamp_conflicts_do_not_create_a_weight_change(sessions):
    await manual(sessions, 70)
    await manual(sessions, 80)
    row = (await call("longitudinal", data_type="weight"))["summaries"][0]
    assert row["source"] == "manuel" and row["statistics"]["count"] == 0
    assert row["change"] is None and "conflicting_values_at_same_time" in row["reason_codes"]
    assert len(row["evidence"]) == 2


async def test_intraday_conflict_and_missing_correction_are_visible(sessions):
    await store(sessions, "steps", steps("a", count=20), steps("b", count=30))
    row = (await call("intraday", data_type="steps"))["summaries"][0]
    assert row["conflict_seconds"] == 3600 and row["supported_seconds"] == 0
    assert "overlapping_intervals" in row["reason_codes"]
    await store(
        sessions,
        "steps",
        steps("a", count=None),
        steps("b", count=None, start=T0 + timedelta(hours=1)),
    )
    row = (await call("intraday", data_type="steps"))["summaries"][0]
    assert row["supported_seconds"] == 0 and row["gap_seconds"] == 86400
    assert row["time_weighted_mean"] is None


async def test_units_methods_and_nonfinite_values_do_not_invent_supported_measurements(sessions):
    await store(
        sessions,
        "daily-respiratory-rate",
        summary(
            "daily-respiratory-rate",
            breathsPerMinute=12,
            unit="unknown",
            measurementMethod="estimated",
        ),
    )
    row = (await call("physiology", data_type="daily-respiratory-rate"))["summaries"][0]
    assert row["state"] == "unsupported" and "unsupported_unit" in row["reason_codes"]
    assert row["method_details"]["measurementMethod"] == "estimated"
    await store(sessions, "daily-vo2-max", summary("daily-vo2-max", vo2Max="Infinity"))
    row = (await call("physiology", data_type="daily-vo2-max"))["summaries"][0]
    assert row["statistics"]["count"] == 0 and row["state"] == "unsupported"
    json.dumps(row, allow_nan=False)


async def test_new_member_and_byte_budget_are_detected(sessions, monkeypatch):
    result = await call("longitudinal", limit=1)
    await manual(sessions, 70)
    assert (await call("longitudinal", limit=1, cursor=result["next_cursor"]))[
        "code"
    ] == "stale_cursor"
    await resting(sessions, [50])
    monkeypatch.setattr(interpretation_inputs, "MAX_INPUT_BYTES", 1)
    assert (await call("baseline"))["code"] == "input_budget_exceeded"


async def test_distinct_payload_methods_stay_separate_with_equal_values(sessions):
    await store(
        sessions,
        "daily-oxygen-saturation",
        summary("daily-oxygen-saturation", "a", averagePercentage=95, measurementMethod="A"),
        summary("daily-oxygen-saturation", "b", averagePercentage=95, measurementMethod="B"),
    )
    rows = (await call("baseline", metric="spo2"))["summaries"]
    assert len(rows) == 2 and len({r["definition"] for r in rows}) == 2
    assert {r["method_details"]["measurementMethod"] for r in rows} == {"A", "B"}
    assert all(len(r["evidence"]) == 1 for r in rows)


async def test_longitudinal_overlap_and_window_crossing_keep_original_scope(sessions):
    for name, start, end in [
        ("a", T0 - timedelta(hours=1), T0 + timedelta(hours=1)),
        ("b", T0, T0 + timedelta(hours=2)),
    ]:
        await store(
            sessions,
            "total-calories",
            point(
                "total-calories",
                name,
                {
                    "interval": {"startTime": start.isoformat(), "endTime": end.isoformat()},
                    "kcal": 100,
                },
            ),
        )
    row = (await call("longitudinal", data_type="total-calories"))["summaries"][0]
    assert row["statistics"]["count"] == 2
    assert row["change"] is None and "overlapping_intervals" in row["reason_codes"]
    assert row["evidence"][0]["start"] == (T0 - timedelta(hours=1)).isoformat()
    assert row["evidence"][0]["value"] == 100


async def test_amounts_with_different_interval_scopes_are_separate(sessions):
    for hours in (1, 2):
        await store(
            sessions,
            "active-energy-burned",
            point(
                "active-energy-burned",
                str(hours),
                {
                    "interval": {
                        "startTime": T0.isoformat(),
                        "endTime": (T0 + timedelta(hours=hours)).isoformat(),
                    },
                    "kcal": 100,
                },
            ),
        )
    rows = (await call("longitudinal", data_type="active-energy-burned"))["summaries"]
    assert len(rows) == 2 and {r["interval_seconds"] for r in rows} == {3600, 7200}
    assert all(r["statistics"]["count"] == 1 and r["change"] is None for r in rows)
