"""Only observed stage timestamps form a timeline; estimates keep their assumptions."""

from datetime import datetime, timedelta
from itertools import pairwise
from random import Random
from types import SimpleNamespace

import pytest
from derived_records import BAND, DAY, PHONE, T0, read, sleep, stage, store, streams

from app.services.sleep_timeline import sleep_timeline


def test_timeline_matches_original_cell_scan():
    rng = Random(123)
    for size in range(30):
        pairs = []
        for _ in range(size):
            a = rng.randrange(12)
            b = a + rng.randrange(6)
            timed = [
                stage(
                    rng.choice(["deep", "wake", "rem", "other"]),
                    rng.randrange(a - 2, b + 1),
                    rng.randrange(a, b + 3),
                )
                for _ in range(5)
            ]
            row = SimpleNamespace(
                start_time=T0 + timedelta(hours=a), end_time=T0 + timedelta(hours=b)
            )
            pairs.append((row, {"stages": timed + timed[:1]}))
        result = sleep_timeline(pairs)
        # Reuse parsed evidence, but independently reproduce the original cell scan.
        spans = [(r.start_time, r.end_time) for r, _ in pairs if r.end_time > r.start_time]
        stages = [
            (datetime.fromisoformat(c["start"]), datetime.fromisoformat(c["end"]), c["stage"])
            for s in result["sessions"]
            for c in s["timeline"] or []
        ]
        edges = sorted(
            {x for a, b in spans for x in (a, b)} | {x for a, b, _ in stages for x in (a, b)}
        )
        cells, totals = [], {}
        for left, right in pairwise(edges):
            active = sum(a < right and b > left for a, b in spans)
            labels = [label for a, b, label in stages if a < right and b > left]
            distinct = sorted(set(labels))
            state = "between_sessions" if not active else "known"
            if active and (not labels or distinct == ["unknown"]):
                state = "unknown"
            if len(distinct) > 1:
                state = "conflict"
            if state == "known":
                totals[distinct[0]] = totals.get(distinct[0], 0) + (right - left).total_seconds()
            cells.append(
                {
                    "start": left.isoformat(),
                    "end": right.isoformat(),
                    "state": state,
                    "stages": distinct,
                    "session_count": active,
                    "stage_count": len(labels),
                }
            )
        assert result["timeline"] == (cells if stages else None)
        assert result["timeline_stage_seconds"] == (totals if stages else None)
        assert result["known_seconds"] == sum(totals.values())


async def test_split_sessions_have_episode_boundaries_and_between_session_gaps(sessions):
    await store(
        sessions,
        "sleep",
        sleep("first", stages=[stage("LIGHT", 1, 2), stage("AWAKE", 2, 3)]),
        sleep(
            "second",
            start=T0 + timedelta(hours=4),
            end=T0 + timedelta(hours=5),
            stages=[stage("DEEP", 4, 5)],
        ),
    )
    result = streams(await read(sessions), "sleep_seconds")[0]
    timeline = result["sleep"]
    assert len(timeline["sessions"]) == len(timeline["episodes"]) == 2
    assert timeline["between_sessions_seconds"] == 3600
    assert timeline["unknown_seconds"] == timeline["stage_overlap_seconds"] == 0
    assert [cell["state"] for cell in timeline["timeline"]] == [
        "known",
        "known",
        "between_sessions",
        "known",
    ]
    assert timeline["timeline_stage_seconds"] == {"light": 3600, "awake": 3600, "deep": 3600}
    assert result["value"] == 7200
    # The second session has no awake entry; a known DEEP interval must not undo it.
    assert "awake_unknown" in result["uncertainties"]


@pytest.mark.parametrize("container", ["stages", "sleepStages", "wrapped_stages", "wrapped_data"])
async def test_supported_timed_shapes_keep_unknown_intervals(sessions, container):
    entries = [stage("DEEP", 1, 1.5), stage("REM", 2, 2.5)]
    payload = (
        {"stages": {container.removeprefix("wrapped_"): entries}}
        if container.startswith("wrapped_")
        else {container: entries}
    )
    await store(sessions, "sleep", sleep(**payload))
    result = streams(await read(sessions), "sleep_seconds")[0]
    timeline = result["sleep"]
    assert timeline["unknown_seconds"] == 3600
    assert timeline["timed_asleep_seconds"] == 3600
    assert timeline["estimated_asleep_seconds"] == 7200
    assert timeline["estimate_minus_timed_asleep_seconds"] == 3600
    assert timeline["stage_total_differences_seconds"] == {"deep": 0, "rem": 0}
    assert "awake_unknown" in result["uncertainties"]


async def test_conflicting_stages_and_overlapping_sessions_are_not_silently_resolved(sessions):
    await store(
        sessions,
        "sleep",
        sleep("a", stages=[stage("LIGHT", 1, 3), stage("AWAKE", 2, 2.5)]),
        sleep(
            "b",
            start=T0 + timedelta(hours=2),
            end=T0 + timedelta(hours=4),
            stages=[stage("DEEP", 2, 4)],
        ),
    )
    result = streams(await read(sessions), "sleep_seconds")[0]
    timeline = result["sleep"]
    assert timeline["session_overlap_seconds"] == timeline["stage_overlap_seconds"] == 3600
    assert timeline["timeline_stage_seconds"] == {"light": 3600, "deep": 3600}
    assert any(
        cell["stage_count"] == 3 and cell["state"] == "conflict" for cell in timeline["timeline"]
    )
    assert {"conflicting_stages", "overlap_estimated"} <= set(result["uncertainties"])
    assert len(timeline["episodes"]) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"summary": {"minutesAsleep": 0, "stagesSummary": [{"type": "AWAKE", "minutes": 120}]}},
        {
            "stages": [
                {"stage": "awake", "duration": "0s"},
                {"stage": "restless", "duration": "600s"},
            ]
        },
        {"sleepStages": {"unknownSchema": [stage("LIGHT", 1, 3)]}},
        {},
    ],
)
async def test_totals_or_unsupported_shapes_never_invent_a_timeline(sessions, payload):
    await store(sessions, "sleep", sleep(**payload))
    result = streams(await read(sessions), "sleep_seconds")[0]
    timeline = result["sleep"]
    assert timeline["timeline"] is None and timeline["sessions"][0]["timeline"] is None
    assert timeline["unknown_seconds"] == 7200
    assert timeline["timeline_stage_seconds"] is None
    assert "timeline_unavailable" in timeline["reason_codes"]
    if "summary" in payload:
        assert result["value"] == 0
    elif "stages" in payload:
        assert result["value"] == 6600  # restless subtraction stays in force
    else:
        assert result["value"] == 7200 and "awake_unknown" in result["uncertainties"]
    assert "from_normalized_columns" not in result["observations"][0]["assumptions"]


async def test_invalid_session_is_no_value_and_does_not_hide_measured_zero(sessions):
    await store(
        sessions,
        "sleep",
        sleep("invalid", start=T0 + timedelta(hours=4), end=T0 + timedelta(hours=3)),
    )
    assert streams(await read(sessions), "sleep_seconds") == []
    await store(sessions, "sleep", sleep("zero", summary={"minutesAsleep": 0}))
    result = streams(await read(sessions), "sleep_seconds")[0]
    assert result["value"] == 0 and len(result["sleep"]["sessions"]) == 1


async def test_session_summary_and_stage_union_disagree_explicitly(sessions):
    await store(
        sessions,
        "sleep",
        sleep(
            summary={
                "minutesAsleep": 30,
                "stagesSummary": [
                    {"type": "DEEP", "minutes": 100},
                    {"type": "AWAKE", "minutes": 20},
                ],
            },
            stages=[stage("DEEP", 1, 2), stage("AWAKE", 2, 3)],
        ),
    )
    result = streams(await read(sessions), "sleep_seconds")[0]
    timeline = result["sleep"]
    assert result["value"] == 1800
    assert timeline["sessions"][0]["supplied_asleep_seconds"] == 1800
    assert timeline["stage_total_differences_seconds"] == {"deep": -2400, "awake": 2400}
    assert timeline["estimate_minus_timed_asleep_seconds"] == -1800


async def test_stage_outside_session_and_unknown_labels_are_visible(sessions):
    await store(sessions, "sleep", sleep(stages=[stage("DEEP", 0, 2), stage("UNSUPPORTED", 2, 3)]))
    timeline = streams(await read(sessions), "sleep_seconds")[0]["sleep"]
    assert "stage_outside_session" in timeline["reason_codes"]
    assert timeline["unknown_seconds"] == 3600
    assert timeline["timeline_stage_seconds"] == {"deep": 3600}


@pytest.mark.parametrize("duration", [None, "1e20s"])
async def test_sleep_sources_remain_separate_and_wake_date_follows_timezone(sessions, duration):
    end = T0 + timedelta(hours=1)
    start = T0 - timedelta(hours=1)
    await store(
        sessions,
        "sleep",
        sleep("band", start, end, BAND, duration=duration, summary={"minutesAsleep": 60}),
        sleep("phone", start, end, PHONE, duration=duration, summary={"minutesAsleep": 90}),
    )
    card = await read(sessions, DAY - timedelta(days=1), DAY, "Pacific/Honolulu")
    assert sorted(row["value"] for row in streams(card, "sleep_seconds")) == [3600, 5400]
    assert streams(card, "sleep_seconds", 1) == []
