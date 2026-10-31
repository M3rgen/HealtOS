"""Bound executed sweep operations, including scans that do not compare timestamps."""

import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.dashboard import sleep_estimate
from app.services.sleep_timeline import sleep_timeline


@pytest.mark.parametrize("shape", ["disjoint", "nested", "dense"])
@pytest.mark.parametrize("kind", ["sessions", "stages"])
def test_twenty_thousand_intervals_have_bounded_sweep_work(shape, kind):
    count = 20_000
    origin = datetime(2026, 9, 29, tzinfo=UTC)
    spans = [
        (2 * i, 2 * i + 1)
        if shape == "disjoint"
        else (i, 2 * count - i)
        if shape == "nested"
        else (i, i + count)
        for i in reversed(range(count))
    ]
    pairs, entries = [], []
    for a, b in spans:
        left, right = origin + timedelta(seconds=a), origin + timedelta(seconds=b)
        row = SimpleNamespace(
            start_time=left, end_time=right, duration_seconds=b - a, awake_seconds=(b - a) / 2
        )
        pairs.append((row, None))
        entries.append(dict(startTime=left.isoformat(), endTime=right.isoformat(), stage="deep"))
    if kind == "stages":
        row = SimpleNamespace(start_time=origin, end_time=origin + timedelta(seconds=2 * count))
        pairs = [(row, {"stages": entries})]
    files = {sleep_estimate.__code__.co_filename, sleep_timeline.__code__.co_filename}
    operations, budget = 0, 16 * count * count.bit_length()

    def trace(frame, event, arg):
        nonlocal operations
        if frame.f_code.co_filename not in files:
            return None
        if event == "line":
            operations += 1
            assert operations < budget, "sleep sweep exceeded O(n log n) operation budget"
        return trace

    previous = sys.gettrace()
    try:
        sys.settrace(trace)
        estimate, timeline = sleep_estimate(pairs), sleep_timeline(pairs)
    finally:
        sys.settrace(previous)
    union = count if shape == "disjoint" else 2 * count - (shape == "dense")
    assert estimate.seconds == (2 * count if kind == "stages" else union / 2)
    assert timeline["covered_session_seconds"] == (2 * count if kind == "stages" else union)
    if kind == "stages":
        assert len(timeline["sessions"][0]["timeline"]) == count
        assert timeline["timeline_stage_seconds"] == {"deep": union}
        assert len(timeline["timeline"]) >= count
        assert max(c["stage_count"] for c in timeline["timeline"]) == (
            1 if shape == "disjoint" else count
        )
        expected_overlap = 0 if shape == "disjoint" else 2 * count - 2 - (shape == "dense")
        assert timeline["stage_overlap_seconds"] == expected_overlap
    assert 0 < operations < budget
