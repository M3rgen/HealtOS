"""Retrieval part of the Umay coverage card (schema 2), read from append-only receipts.

Each closed day of a metric's Google data type gets the state of
app/services/retrieval_coverage.py. A day *without a value* is then one of:

* ``empty_response``: complete receipt chains covered the whole day and Google returned
  nothing for it (the strongest "there was nothing to fetch");
* ``returned_without_value``: Google returned records for the day, yet none of them gives
  a counted value (excluded, unusable, or a record Google keeps rewriting under one id);
* ``not_retrieved``: no read touched the day although receipts were already being written;
* ``incomplete``: a read touched the day but did not prove all of it (failed, cut short);
* ``unknown``: before receipts began, or beyond the evidence budget.

The reader's budgets are kept: a window over them is read in smaller pieces, and a single
day that is still over them stays unknown (``retrieval_evidence_limit``). Nothing writes.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.google_health.data_types import REGISTRY
from app.services.coverage import Metric, Window
from app.services.retrieval_coverage import (
    MAX_DAYS,
    CoverageLimit,
    daily_evidence,
    evidence,
)

WITHOUT_VALUE = (
    "empty_response",
    "returned_without_value",
    "not_retrieved",
    "incomplete",
    "unknown",
)
NAMES = {
    "completed_empty": "empty_response",
    "completed_with_records": "returned_without_value",
    "not_attempted": "not_retrieved",
    "incomplete": "incomplete",
}
# Evidence reads per metric; one card never runs an unbounded number of them.
MAX_PIECES = 64
# Gap periods listed for a single metric; the rest is counted in ``gaps_omitted``.
MAX_GAPS = 31
DAY = timedelta(days=1)


async def day_states(session, name: str, window: Window, horizon) -> tuple[dict, bool]:
    """The retrieval state of every closed day; True when a budget left some unknown."""
    spec = REGISTRY[name]
    last = min(window.end, window.today - DAY)
    states: dict[date, str] = {}
    start, span, pieces, limited = window.first, MAX_DAYS, 0, False
    while start <= last:
        if pieces >= MAX_PIECES:
            limited = True
            break
        pieces += 1
        end = min(start + (span - 1) * DAY, last)
        piece = Window(start, end, window.today, window.zone)
        try:
            attempts, times = await evidence(session, name, piece)
        except CoverageLimit:
            if span > 1:
                span = max(1, span // 2)
            else:
                limited = True
                start += DAY
            continue
        day = start
        while day <= end:
            states[day] = daily_evidence(spec, day, piece, attempts, times, horizon)[0]
            day += DAY
        del attempts, times
        start, span = end + DAY, min(MAX_DAYS, span * 2)
    return states, limited


def gap_periods(closed: list[date], values: set[date], states: dict) -> list[dict]:
    """Runs of consecutive closed days without a value that share one state."""
    runs: list[dict] = []
    for day in closed:
        if day in values:
            continue
        state = NAMES.get(states.get(day, "unknown"), "unknown")
        last = runs[-1] if runs else None
        if last and last["state"] == state and last["end_date"] + DAY == day:
            last["end_date"], last["days"] = day, last["days"] + 1
        else:
            runs.append({"start_date": day, "end_date": day, "days": 1, "state": state})
    return runs


async def metric_retrieval(
    session, metric: Metric, window: Window, values: set[date], horizon, gaps: bool
) -> tuple[dict, list[str]]:
    """Fields of the card's ``Retrieval`` and the metric's reason codes."""
    closed = []
    day = window.first
    while day <= window.end and day < window.today:
        closed.append(day)
        day += DAY
    if metric.data_type is None or horizon is None:
        return {"state": "unknown", "unknown_days": len(closed)}, ["retrieval_unknown"]
    states, limited = await day_states(session, metric.data_type, window, horizon)
    counts = {key: 0 for key in ("completed_with_records", "completed_empty")}
    counts |= {"incomplete": 0, "not_attempted": 0, "unknown": 0}
    without: dict[str, int] = dict.fromkeys(WITHOUT_VALUE, 0)
    for day in closed:
        state = states.get(day, "unknown")
        counts[state if state in counts else "unknown"] += 1
        if day not in values:
            without[NAMES.get(state, "unknown")] += 1
    codes = []
    if counts["unknown"]:
        codes.append("retrieval_unknown")
    if limited:
        codes.append("retrieval_evidence_limit")
    if without["not_retrieved"]:
        codes.append("days_not_retrieved")
    if without["returned_without_value"]:
        codes.append("returned_without_value")
    result = {
        "state": "receipts",
        "completed_with_records_days": counts["completed_with_records"],
        "completed_empty_days": counts["completed_empty"],
        "incomplete_days": counts["incomplete"],
        "not_attempted_days": counts["not_attempted"],
        "unknown_days": counts["unknown"],
        "without_value": without,
    }
    if gaps:
        runs = gap_periods(closed, values, states)
        result["gaps"] = runs[:MAX_GAPS]
        result["gaps_omitted"] = max(0, len(runs) - MAX_GAPS)
    return result, codes
