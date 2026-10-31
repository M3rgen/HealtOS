"""Session computation bounds through the production reader and synthetic archive."""

from datetime import timedelta
from unittest.mock import Mock

import pytest
from derived_records import BAND, T0, snapshot, store
from interpretation_records import SCOPE_ERROR, call, exercise
from interpretation_records import production_reader as production_reader

from app.models.manual import ManualWorkout
from app.parsers.common import payload_hash
from app.services import health_interpretation
from app.services import interpretation_sessions as sessions_service


async def seed(sessions, count, *, disjoint=False):
    records = [
        exercise(
            str(index),
            interval={
                "startTime": (T0 + timedelta(seconds=index * 2 if disjoint else 0)).isoformat(),
                "endTime": (
                    T0 + timedelta(seconds=index * 2 + 1 if disjoint else 3600)
                ).isoformat(),
            },
        )
        for index in range(count)
    ]
    await store(sessions, "exercise", *records)
    await store(sessions, "exercise", {**records[0], "syntheticCorrection": True})


def assert_refusal(result, code):
    assert result["code"] == code and result["reason_codes"] == [code]
    if code.endswith("_budget_exceeded"):
        assert result["error"] == SCOPE_ERROR
    assert result["schema_version"] == "2" and "error" in result
    assert "summaries" not in result and "next_cursor" not in result


async def test_summary_limit_stops_generation_before_overlap_scan_or_models(sessions, monkeypatch):
    count = health_interpretation.MAX_SUMMARIES + 10
    await seed(sessions, count)
    async with sessions() as session:
        before = await snapshot(session)
    original = sessions_service._session_rows
    generated = []

    def rows(*args):
        for row in original(*args):
            generated.append(row)
            yield row

    monkeypatch.setattr(sessions_service, "_session_rows", rows)
    scan = Mock(side_effect=AssertionError("summary budget must precede overlap scan"))
    model = Mock(side_effect=AssertionError("summary budget must precede models"))
    monkeypatch.setattr(sessions_service, "_session_overlaps", scan)
    monkeypatch.setattr(sessions_service, "SessionSummary", model)
    for filters in (
        {},
        {"source": "kaynak-" + payload_hash(BAND)},
        {"definition": "google_exercise_session"},
    ):
        generated.clear()
        assert_refusal(await call("sessions", limit=1, **filters), "summary_budget_exceeded")
        assert len(generated) == health_interpretation.MAX_SUMMARIES + 1 < count
    assert (await call("sessions", source="absent"))["total_summaries"] == 0
    assert (await call("sessions", definition="absent"))["total_summaries"] == 0
    scan.assert_not_called()
    model.assert_not_called()
    async with sessions() as session:
        assert await snapshot(session) == before


async def test_dense_overlap_budget_stops_before_models_and_preserves_outside_sources(
    sessions, monkeypatch
):
    count = 600
    await seed(sessions, count)
    async with sessions.begin() as session:
        session.add_all(
            [
                ManualWorkout(exercise_type="run", start_time=T0, end_time=T0 + timedelta(hours=1))
                for _ in range(2)
            ]
        )
    async with sessions() as session:
        before = await snapshot(session)
    with monkeypatch.context() as patch:
        model = Mock(side_effect=AssertionError("overlap budget must precede models"))
        push = Mock(wraps=sessions_service.heappush)
        patch.setattr(sessions_service, "SessionSummary", model)
        patch.setattr(sessions_service, "heappush", push)
        assert_refusal(await call("sessions", limit=1), "overlap_budget_exceeded")
        visited = push.call_count
        budget = sessions_service.MAX_OVERLAP_REFERENCES
        assert visited * (visited - 1) <= budget < visited * (visited + 1)
        assert visited < count
        model.assert_not_called()
    # Unselected Google sessions overlap each other, but only returned references count.
    exact_budget = 2 * (count + 1)
    monkeypatch.setattr(health_interpretation, "MAX_SUMMARIES", 2)
    monkeypatch.setattr(sessions_service, "MAX_OVERLAP_REFERENCES", exact_budget)
    for filters in ({"source": "manuel"}, {"definition": "manual_workout_session"}):
        page = await call("sessions", limit=1, **filters)
        assert page["total_summaries"] == 2 and page["next_cursor"]
        assert len(page["summaries"][0]["overlaps"]) == count + 1
        rest = await call("sessions", cursor=page["next_cursor"], limit=2, **filters)
        assert rest["summary_offset"] == 1 and rest["next_cursor"] is None
        assert len(rest["summaries"][0]["overlaps"]) == count + 1
    monkeypatch.setattr(sessions_service, "MAX_OVERLAP_REFERENCES", exact_budget - 1)
    assert_refusal(await call("sessions", source="manuel", limit=1), "overlap_budget_exceeded")
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize("count", [400, 1000])
async def test_disjoint_intervals_have_subquadratic_comparisons(sessions, monkeypatch, count):
    await seed(sessions, count, disjoint=True)
    original = sessions_service._session_rows
    comparisons = 0

    class Stamp(str):
        def __lt__(self, other):
            nonlocal comparisons
            comparisons += 1
            return super().__lt__(other)

        def __le__(self, other):
            nonlocal comparisons
            comparisons += 1
            return super().__le__(other)

        def __gt__(self, other):
            nonlocal comparisons
            comparisons += 1
            return super().__gt__(other)

    def rows(*args):
        for row in original(*args):
            yield {**row, "start": Stamp(row["start"]), "end": Stamp(row["end"])}

    monkeypatch.setattr(sessions_service, "_session_rows", rows)
    monkeypatch.setattr(sessions_service, "MAX_OVERLAP_REFERENCES", 0)
    async with sessions() as session:
        before = await snapshot(session)
    result = await call("sessions", limit=1)
    assert result["total_summaries"] == count
    assert result["summaries"][0]["overlaps"] == []
    assert 0 < comparisons < 30 * count  # Includes sorting; a full pair scan exceeds this.
    async with sessions() as session:
        assert await snapshot(session) == before


async def test_interval_order_touching_filters_and_outside_change_expire_cursor(sessions):
    # Nested intervals, equal starts/ends, touching boundaries and a crossing first day.
    spans = [(1, 4), (1, 3), (2, 3), (3, 5), (5, 6), (-1, 1)]
    await store(
        sessions,
        "exercise",
        *[
            exercise(
                str(index),
                interval={
                    "startTime": (T0 + timedelta(hours=start)).isoformat(),
                    "endTime": (T0 + timedelta(hours=end)).isoformat(),
                },
            )
            for index, (start, end) in enumerate(spans)
        ],
    )
    async with sessions.begin() as session:
        session.add(
            ManualWorkout(exercise_type="run", start_time=T0, end_time=T0 + timedelta(hours=2))
        )
    async with sessions() as session:
        before = await snapshot(session)
    whole = (await call("sessions"))["summaries"]
    keys = [(r["start"], r["end"], r["evidence"][0]["record"]) for r in whole]
    assert keys == sorted(keys)
    for row in whole:
        assert row["overlaps"] == [
            other["evidence"][0]["record"]
            for other in whole
            if other is not row and other["start"] < row["end"] and other["end"] > row["start"]
        ]
        assert ("overlapping_sessions" in row["reason_codes"]) == bool(row["overlaps"])
    google = [row for row in whole if row["definition"] == "google_exercise_session"]
    filters = {"definition": "google_exercise_session"}
    page = await call("sessions", limit=2, **filters)
    rest = await call("sessions", cursor=page["next_cursor"], limit=200, **filters)
    assert page["summaries"] + rest["summaries"] == google
    async with sessions() as session:
        assert await snapshot(session) == before
    # Changing only evidence outside the filter still invalidates the bound cursor.
    async with sessions.begin() as session:
        session.add(
            ManualWorkout(exercise_type="walk", start_time=T0, end_time=T0 + timedelta(hours=3))
        )
    assert_refusal(await call("sessions", cursor=page["next_cursor"], **filters), "stale_cursor")
