"""Civil-day dashboard read model; never modifies source health records."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from heapq import heappop, heappush
from itertools import pairwise
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.timeutil import as_utc
from app.google_health.data_types import supported_data_types
from app.models.eligibility import normalized_counted, normalized_quarantined, raw_counted
from app.models.health import HeartRate, SleepSession, Steps, Weight
from app.models.identity import (
    held_elsewhere,
    holds_live,
    key_held_elsewhere,
    plan_each_query,
    superseded,
)
from app.models.manual import ManualMeasurement
from app.models.state import RawHealthData
from app.parsers.common import first_number, parse_date, payload_for
from app.parsers.sleep import SLEEP_NUMERIC_ERROR, sleep_asleep_seconds
from app.services.current_heart import heart_rate_hours
from app.services.current_sleep import current_sleep, sleep_wake_window
from app.services.current_steps import current_steps
from app.services.current_weight import latest_weight
from app.services.dashboard_window import daily_window
from app.services.finite_math import (
    finite_difference,
    finite_number,
    finite_scale,
    finite_scaled_sum,
)
from app.services.manual_flags import excluded_measurements

# Machine-readable reasons a day's sleep value is not a plain measurement, in this order.
SLEEP_ASSUMPTIONS = (
    "invalid_sessions_skipped",  # sessions ending at or before their start are left out
    SLEEP_NUMERIC_ERROR,  # invalid evidence makes the day's total unavailable
    "from_normalized_columns",  # no readable raw payload: duration less awake columns
    "duration_from_payload",  # period from the payload's top-level duration (no schema)
    "awake_unknown",  # no summary or stages: the whole period, an upper bound
    "overlap_estimated",  # overlapping sessions: the least asleep fraction of each overlap
)


@dataclass(frozen=True)
class SleepEstimate:
    seconds: float | None
    assumptions: list[str] = field(default_factory=list)


def sleep_estimate(sessions: list[tuple[SleepSession, dict | None]]) -> SleepEstimate:
    """One day's asleep total from (session, sleep payload or None) pairs.

    The payload gives asleep time by its shape (`sleep_asleep_seconds`); without one the
    normalized duration less awake columns do. Overlapping sessions are counted once:
    stage totals cannot locate awake intervals, so each overlap takes the least asleep
    fraction; this conservative estimate is exposed. No valid session is no value.

    Ordered boundary events and a lazy minimum heap take O(n log n) time and O(n)
    space. Keep every original boundary so floating-point accumulation is unchanged.
    """
    if not sessions:
        return SleepEstimate(None)
    found: set[str] = set()
    spans = []
    for row, payload in sessions:
        start, end = as_utc(row.start_time), as_utc(row.end_time)
        seconds = (end - start).total_seconds()
        if seconds <= 0:
            found.add("invalid_sessions_skipped")
            continue
        if payload is not None:
            measured = sleep_asleep_seconds(payload, start, end)
            found.update(measured.assumptions)
            asleep = finite_number(
                measured.period if measured.seconds is None else measured.seconds
            )
        else:
            found.add("from_normalized_columns")
            if row.awake_seconds is None:
                found.add("awake_unknown")
            duration = (
                finite_number(row.duration_seconds) if row.duration_seconds is not None else seconds
            )
            awake = finite_number(row.awake_seconds) if row.awake_seconds is not None else 0.0
            asleep = (
                finite_difference(duration, awake)
                if duration is not None and awake is not None
                else None
            )
        if asleep is None:
            found.add(SLEEP_NUMERIC_ERROR)
            continue
        fraction = finite_scale(max(0, min(seconds, asleep)), 1.0, seconds)
        if fraction is None:
            found.add(SLEEP_NUMERIC_ERROR)
            continue
        spans.append((start, end, fraction))
    events: dict[datetime, list[tuple[int, float, datetime]]] = {}
    for start, end, fraction in spans:
        events.setdefault(start, []).append((1, fraction, end))
        events.setdefault(end, []).append((-1, fraction, end))
    # Each span enters the minimum heap once and leaves at most once. Expired
    # nonminimum entries may stay buried until needed; no active set is rescanned.
    minimums: list[tuple[float, datetime]] = []
    active = 0
    total = 0.0
    for left, right in pairwise(sorted(events)):
        # End events precede starts at equal times: intervals are half open.
        for delta, fraction, end in sorted(events[left]):
            active += delta
            if delta > 0:
                heappush(minimums, (fraction, end))
        # A minimum may have ended even when another span keeps active nonzero.
        while minimums and minimums[0][1] <= left:
            heappop(minimums)
        if active:
            fraction = minimums[0][0]
            total += (right - left).total_seconds() * fraction
            if active > 1 and fraction < 1:
                found.add("overlap_estimated")
    return SleepEstimate(
        total if spans and SLEEP_NUMERIC_ERROR not in found else None,
        [code for code in SLEEP_ASSUMPTIONS if code in found],
    )


def sleep_total(sessions: list[SleepSession]) -> float | None:
    """Asleep total from normalized columns alone; see `sleep_estimate`."""
    return sleep_estimate([(row, None) for row in sessions]).seconds


async def dashboard_data(
    session: AsyncSession,
    end_date: date,
    days: int,
    timezone: str,
    *,
    valid_sources_only: bool = False,
    include_source_deleted: bool = False,
) -> dict:
    zone = ZoneInfo(timezone)
    window = daily_window(end_date, days, zone)
    first, start, stop = window.first, window.start, window.stop
    await plan_each_query(session)
    today = datetime.now(zone).date()
    rows = {
        first + timedelta(days=i): {
            "date": (first + timedelta(days=i)).isoformat(),
            "steps": None,
            "sleep_seconds": None,
            "sleep_assumptions": [],
            "resting_bpm": None,
            "hrv_ms": None,
            "spo2": None,
            "partial": first + timedelta(days=i) == today,
        }
        for i in range(days)
    }
    step_days: dict[date, list[tuple[float, float, float]]] = {}
    step_buckets: dict[int, list[tuple[float, float, float]]] = {}

    # Every reader counts the same records (app/models/eligibility.py, ``normalized_counted`` and
    # ``raw_counted``): a live raw row, neither it nor its normalized row quarantined, not
    # superseded by a counted row (migration 20261021_0012). A normalized row without a raw
    # row is not counted. ``valid_sources_only`` (kept for older callers) asks for exactly
    # this, which is the default: it only overrides ``include_source_deleted``. With source
    # deletions included, a deleted row counts too, unless it is superseded or a live
    # duplicate it holds counts instead (never a deleted holder's value in place of a live
    # row's).
    include_source_deleted = include_source_deleted and not valid_sources_only
    any_superseded = or_(
        held_elsewhere(RawHealthData.id),
        and_(
            RawHealthData.source_deleted_at.is_not(None),
            or_(superseded(RawHealthData.id), holds_live(RawHealthData.id)),
        ),
    )

    def valid_source(model, data_type: str):
        if not include_source_deleted:
            return normalized_counted(model, (data_type,))
        raw = select(RawHealthData.id).where(
            RawHealthData.data_type == data_type,
            RawHealthData.google_data_point_id == model.google_data_point_id,
            RawHealthData.is_quarantined.is_(False),
        )
        deleted_and_held = raw.where(
            RawHealthData.source_deleted_at.is_not(None),
            or_(superseded(RawHealthData.id), holds_live(RawHealthData.id)),
        )
        return and_(
            raw.exists(),
            ~key_held_elsewhere(data_type, model.google_data_point_id),
            ~deleted_and_held.exists(),
        )

    def raw_valid(data_types):
        if include_source_deleted:
            # A quarantine on either side still hides the record (only deletion is let in).
            return and_(
                RawHealthData.is_quarantined.is_(False),
                ~any_superseded,
                *(~clause for clause in normalized_quarantined(RawHealthData, data_types)),
            )
        return raw_counted(data_types=data_types)

    # Split source intervals at civil-hour boundaries, including midnight and DST.
    step_raw = aliased(RawHealthData)  # valid_source probes the raw table itself
    steps = (
        await session.scalars(
            select(step_raw.raw_json)
            .select_from(Steps)
            .join(
                step_raw,
                and_(
                    step_raw.data_type == "steps",
                    step_raw.google_data_point_id == Steps.google_data_point_id,
                ),
            )
            .where(
                step_raw.source_end_time > start,
                step_raw.source_start_time < stop,
                Steps.is_quarantined.is_(False),
                valid_source(Steps, "steps"),
            )
        )
    ).all()
    for record in steps:
        measurement = current_steps(record)
        if measurement is None:
            continue
        left, right, supplied = measurement
        duration = (right - left).total_seconds()
        if duration <= 0:
            continue
        cursor = max(left, start)
        while cursor < min(right, stop):
            local = cursor.astimezone(zone)
            # Split at local-hour boundaries, including zones with fractional offsets.
            edge = min(
                cursor
                + timedelta(
                    seconds=3600 - local.minute * 60 - local.second - local.microsecond / 1e6
                ),
                right,
                stop,
            )
            term = (supplied, (edge - cursor).total_seconds(), duration)
            day = local.date()
            step_days.setdefault(day, []).append(term)
            if day == end_date:
                step_buckets.setdefault(local.hour, []).append(term)
            cursor = edge
    # Evaluate complete buckets: an overflow must not reset the running total, and
    # cancellation may recover a finite result after an overflowing intermediate sum.
    for day, terms in step_days.items():
        rows[day]["steps"] = finite_scaled_sum(terms)
    step_hours = [
        finite_scaled_sum(step_buckets[h]) if h in step_buckets else None for h in range(24)
    ]

    # Each session's own raw record (same key; storage keys both alike) carries the shape
    # asleep time is read from, so rows normalized before a rule change need no rewrite.
    # Joined under an alias: valid_source's subqueries must not correlate to it.
    sleep_raw = aliased(RawHealthData)
    sleep = (
        await session.execute(
            select(sleep_raw.raw_json)
            .select_from(SleepSession)
            .join(
                sleep_raw,
                and_(
                    sleep_raw.data_type == "sleep",
                    sleep_raw.google_data_point_id == SleepSession.google_data_point_id,
                ),
            )
            .where(
                *sleep_wake_window(sleep_raw.source_end_time, start, stop),
                SleepSession.is_quarantined.is_(False),
                valid_source(SleepSession, "sleep"),
            )
            .order_by(sleep_raw.source_end_time)
        )
    ).all()
    by_day: dict[date, list[tuple[SleepSession, dict | None]]] = {}
    for (raw_json,) in sleep:
        measurement = current_sleep(raw_json)
        if measurement is None:
            continue
        left, right, payload = measurement
        # A detached read-only span keeps archive columns untouched, even in the identity map.
        span = SleepSession(start_time=left, end_time=right)
        day = right.astimezone(zone).date()
        if day in rows:
            by_day.setdefault(day, []).append((span, payload))
    for day, values in rows.items():
        estimate = sleep_estimate(by_day.get(day, []))
        values["sleep_seconds"] = estimate.seconds
        values["sleep_assumptions"] = estimate.assumptions
        if values["steps"] is not None:
            values["steps"] = round(values["steps"])

    # Daily summaries have civil dates: do not average them with intraday samples.
    metrics = {
        "daily-resting-heart-rate": ("resting_bpm", ("beatsPerMinute",)),
        "daily-heart-rate-variability": (
            "hrv_ms",
            (
                "averageHeartRateVariabilityMilliseconds",
                "rootMeanSquareOfSuccessiveDifferencesMilliseconds",
                "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds",
                "rmssd",
            ),
        ),
        "daily-oxygen-saturation": ("spo2", ("averagePercentage", "percentage", "average")),
    }
    specs = supported_data_types()
    daily = (
        await session.scalars(
            select(RawHealthData)
            .where(
                RawHealthData.data_type.in_(metrics),
                RawHealthData.source_start_time >= datetime.combine(first, time.min, UTC),
                RawHealthData.source_start_time < datetime.combine(window.following, time.min, UTC),
                raw_valid(tuple(metrics)),
            )
            .order_by(RawHealthData.last_seen_at, RawHealthData.google_data_point_id)
        )
    ).all()
    for item in daily:
        payload = payload_for(specs[item.data_type], item.raw_json)
        day = parse_date(payload.get("date"))
        if day is not None and day in rows:
            key, fields = metrics[item.data_type]
            value = first_number(payload, fields)
            if value is not None:
                rows[day][key] = finite_number(value)

    heart_hours = await heart_rate_hours(
        session, end_date, zone, valid_source(HeartRate, "heart-rate")
    )

    measurements = []
    weight = await latest_weight(session, stop, valid_source(Weight, "weight"))
    if weight is not None:
        measured_at, kilograms = weight
        measurements.append(
            {
                "metric": "weight",
                "value": kilograms,
                "measured_at": measured_at,
                "source": "Google",
            }
        )
    for data_type, metric, fields, scale in (
        ("height", "height", ("heightMillimeters",), 0.1),
        ("body-fat", "body_fat", ("percentage",), 1),
    ):
        item = await session.scalar(
            select(RawHealthData)
            .where(
                RawHealthData.data_type == data_type,
                RawHealthData.source_start_time < stop,
                raw_valid((data_type,)),
            )
            .order_by(RawHealthData.source_start_time.desc())
            .limit(1)
        )
        if item:
            supplied = first_number(payload_for(specs[data_type], item.raw_json), fields)
            if supplied is not None:
                measurements.append(
                    {
                        "metric": metric,
                        "value": finite_scale(supplied, scale),
                        "measured_at": as_utc(item.source_start_time),
                        "source": "Google",
                    }
                )
    for metric in ("height", "weight", "body_fat", "muscle"):
        item = await session.scalar(
            select(ManualMeasurement)
            .where(
                ManualMeasurement.metric == metric,
                ManualMeasurement.measured_at < stop,
                # A measurement flagged as a test entry: the one before it counts instead.
                ManualMeasurement.id.not_in(excluded_measurements()),
            )
            .order_by(ManualMeasurement.measured_at.desc(), ManualMeasurement.created_at.desc())
            .limit(1)
        )
        if item:
            measurements.append(
                {
                    "metric": metric,
                    "value": finite_number(item.value),
                    "measured_at": as_utc(item.measured_at),
                    "source": "Manuel",
                }
            )
    profile = {}
    for item in measurements:
        current = profile.get(item["metric"])
        if current is None or item["measured_at"] >= current["measured_at"]:
            profile[item["metric"]] = item
    for values in rows.values():
        present = sum(
            values[key] is not None
            for key in ("steps", "sleep_seconds", "resting_bpm", "hrv_ms", "spo2")
        )
        values["status"] = (
            "empty"
            if not present
            else "partial"
            if values["partial"]
            else "complete"
            if present == 5
            else "missing"
        )
    return {
        "timezone": timezone,
        "start_date": first,
        "end_date": end_date,
        "days": list(reversed(rows.values())),
        "profile": profile,
        "charts": {
            "steps": step_hours,
            "heart_rate": heart_hours,
        },
        "sleep_method": (
            "Uyanılan güne atanır; uyanık ve huzursuz süre çıkarılır, evre bilgisi yoksa "
            "süre üst sınırdır. Örtüşen oturumlar tek sayılır; örtüşmede süre tahminidir. "
            "Günün varsayımları sleep_assumptions alanındadır."
        ),
    }
