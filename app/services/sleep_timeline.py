"""Evidence-only sleep boundaries; totals never locate stages on a clock."""

from collections import defaultdict
from datetime import datetime
from decimal import DecimalException
from itertools import pairwise

from app.core.timeutil import as_utc
from app.parsers.common import parse_datetime
from app.parsers.sleep import _sleep_stages, sleep_minutes_seconds
from app.services.finite_math import finite_difference, finite_number, finite_sum

STAGES = {"awake", "wake", "restless", "asleep", "light", "deep", "rem"}


def sleep_timeline(sessions: list[tuple]) -> dict:
    """Accept only the explicit startTime/endTime entries supported by synthetic fixtures.

    Each input is (normalized session, payload). Gaps between sessions are separate from
    unknown stages inside a session. The supplied totals and the union of timed stages
    are reported independently, including their differences; neither rewrites the other.

    Boundary events take O((n+m) log(n+m)) time and O(n+m) space for n sessions
    and m stages. Per-cell work depends only on the fixed stage vocabulary.
    """
    spans, stages, details, episodes = [], [], [], []
    reasons = set()

    def supplied_number(value, field):
        result = finite_number(value)
        if value is not None and result is None:
            reasons.add("unsupported_or_missing_value:" + field)
        return result

    for row, payload in sessions:
        start, end = as_utc(row.start_time), as_utc(row.end_time)
        if end <= start:
            reasons.add("invalid_sessions_skipped")
            continue
        spans.append((start, end))
        payload = payload or {}
        entries = payload.get("stages") or payload.get("sleepStages") or []
        if isinstance(entries, dict):
            entries = entries.get("stages") or entries.get("data") or []
        timed = []
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                left = parse_datetime(entry.get("startTime"))
                right = parse_datetime(entry.get("endTime"))
                label = str(entry.get("stage") or entry.get("type") or "").lower()
                label = "awake" if label == "wake" else label
                if left is None or right is None or right <= left:
                    reasons.add("unsupported_stage_timing")
                    continue
                if left < start or right > end:
                    reasons.add("stage_outside_session")
                if label not in STAGES:
                    reasons.add("unknown_stage")
                left, right = max(start, left), min(end, right)
                if left < right:
                    timed.append((left, right, label if label in STAGES else "unknown"))
        stages.extend(timed)
        totals = {
            key: supplied_number(value, "stage_total:" + key)
            for key, value in sorted(_sleep_stages(payload).items())
        }
        summary = payload.get("summary")
        try:
            supplied_asleep = (
                sleep_minutes_seconds(summary.get("minutesAsleep"))
                if isinstance(summary, dict)
                else None
            )
        except DecimalException:
            supplied_asleep = None
            reasons.add("unsupported_or_missing_value:asleep_seconds")
        supplied_asleep = supplied_number(supplied_asleep, "asleep_seconds")
        supplied_period = payload.get("duration")
        if isinstance(supplied_period, str) and supplied_period.endswith("s"):
            supplied_period = supplied_period[:-1]
        details.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "interval_seconds": (end - start).total_seconds(),
                "stage_totals_seconds": totals,
                "supplied_asleep_seconds": supplied_asleep,
                "payload_period_seconds": supplied_number(supplied_period, "period_seconds")
                if supplied_period is not None
                else None,
                "timeline": [
                    {"start": left.isoformat(), "end": right.isoformat(), "stage": label}
                    for left, right, label in sorted(timed)
                ]
                or None,
            }
        )
        if not timed:
            reasons.add("timeline_unavailable")
    spans.sort()
    for left, right in spans:
        if episodes and left <= episodes[-1][1]:
            episodes[-1][1] = max(episodes[-1][1], right)
        else:
            episodes.append([left, right])
    details.sort(key=lambda item: (item["start"], item["end"], str(item["stage_totals_seconds"])))
    result = {
        "sessions": details,
        "episodes": [{"start": a.isoformat(), "end": b.isoformat()} for a, b in episodes],
        "timeline": None,
        "reason_codes": sorted(reasons),
    }
    events: dict[datetime, list[tuple[int, str]]] = defaultdict(list)
    for left, right in spans:
        events[left].append((1, ""))
        events[right].append((-1, ""))
    for left, right, label in stages:
        events[left].append((1, label))
        events[right].append((-1, label))
    cells, totals = [], defaultdict(float)
    active = stage_count = 0
    counts: dict[str, int] = defaultdict(int)
    unknown = conflict = covered = overlap = session_overlap = gaps = 0.0
    for left, right in pairwise(sorted(events)):
        # Remove endings before starts at the boundary, preserving half-open cells.
        for delta, label in sorted(events[left]):
            if not label:
                active += delta
            else:
                stage_count += delta
                counts[label] += delta
                # Presence denotes an active label, not evidence from an earlier cell.
                if not counts[label]:
                    del counts[label]
        duration = (right - left).total_seconds()
        # At most the supported stage vocabulary plus "unknown", regardless of
        # duplicate samples. Multiplicity is retained separately in stage_count.
        distinct = sorted(counts)
        state = (
            "between_sessions"
            if not active
            else "unknown"
            if not stage_count or distinct == ["unknown"]
            else "known"
        )
        if len(distinct) > 1:
            state = "conflict"
        if not active:
            gaps += duration
        elif state == "conflict":
            conflict += duration
        elif state == "unknown":
            unknown += duration
        elif len(distinct) == 1:
            totals[distinct[0]] += duration
        if active:
            covered += duration
        if stage_count > 1:
            overlap += duration
        if active > 1:
            session_overlap += duration
        cells.append(
            {
                "start": left.isoformat(),
                "end": right.isoformat(),
                "state": state,
                "stages": distinct,
                "session_count": active,
                "stage_count": stage_count,
            }
        )
    supplied = defaultdict(list)
    for item in details:
        for label, seconds in item["stage_totals_seconds"].items():
            supplied["awake" if label == "wake" else label].append(seconds)
    differences = {}
    for label, values in sorted(supplied.items()):
        total = finite_sum(values) if None not in values else None
        difference = finite_difference(totals.get(label, 0), total) if total is not None else None
        if difference is None:
            reasons.add("numeric_overflow:stage_total_difference:" + label)
        differences[label] = difference
    result.update(
        timeline=cells if stages else None,
        timeline_stage_seconds=dict(sorted(totals.items())) if stages else None,
        stage_total_differences_seconds=differences if stages else None,
        reason_codes=sorted(reasons),
        unknown_seconds=unknown,
        known_seconds=sum(totals.values()),
        conflict_seconds=conflict,
        covered_session_seconds=covered,
        session_total_seconds=sum(item["interval_seconds"] for item in details),
        stage_overlap_seconds=overlap if stages else None,
        session_overlap_seconds=session_overlap,
        between_sessions_seconds=gaps,
    )
    return result
