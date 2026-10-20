"""Civil-day dashboard read model; never modifies source health records."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import as_utc
from app.google_health.data_types import supported_data_types
from app.models.health import HeartRate, SleepSession, Steps, Weight
from app.models.manual import ManualMeasurement
from app.models.state import RawHealthData
from app.parsers.common import first_number, parse_date, payload_for


def sleep_total(sessions: list[SleepSession]) -> float | None:
    """Remove overlapping sessions instead of double-counting imported duplicates.

    Stage totals cannot locate awake intervals. For overlapping sessions use the
    least asleep fraction over each overlap; this conservative estimate is exposed.
    """
    if not sessions:
        return None
    spans = []
    for row in sessions:
        start, end = as_utc(row.start_time), as_utc(row.end_time)
        seconds = (end - start).total_seconds()
        if seconds <= 0:
            continue
        duration = float(row.duration_seconds) if row.duration_seconds is not None else seconds
        asleep = max(0, min(seconds, duration - float(row.awake_seconds or 0)))
        spans.append((start, end, asleep / seconds))
    edges = sorted({point for start, end, _ in spans for point in (start, end)})
    total = 0.0
    for left, right in zip(edges, edges[1:], strict=False):
        fractions = [fraction for start, end, fraction in spans if start < right and end > left]
        if fractions:
            total += (right - left).total_seconds() * min(fractions)
    return total


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
    first = end_date - timedelta(days=days - 1)
    start = datetime.combine(first, time.min, zone).astimezone(UTC)
    stop = datetime.combine(end_date + timedelta(days=1), time.min, zone).astimezone(UTC)
    today = datetime.now(zone).date()
    rows = {
        first + timedelta(days=i): {
            "date": (first + timedelta(days=i)).isoformat(),
            "steps": None,
            "sleep_seconds": None,
            "resting_bpm": None,
            "hrv_ms": None,
            "spo2": None,
            "partial": first + timedelta(days=i) == today,
        }
        for i in range(days)
    }
    step_hours = [None] * 24

    def valid_source(model, data_type: str):
        raw = select(RawHealthData.id).where(
            RawHealthData.data_type == data_type,
            RawHealthData.google_data_point_id == model.google_data_point_id,
        )
        if valid_sources_only:
            return raw.where(
                RawHealthData.is_quarantined.is_(False),
                RawHealthData.source_deleted_at.is_(None),
            ).exists()
        if include_source_deleted:
            return true()
        # Normalized rows outlive a source deletion; hide them unless asked for.
        return ~raw.where(RawHealthData.source_deleted_at.is_not(None)).exists()

    raw_not_deleted = (
        true()
        if include_source_deleted and not valid_sources_only
        else RawHealthData.source_deleted_at.is_(None)
    )

    # Split source intervals at civil-hour boundaries, including midnight and DST.
    steps = (
        await session.scalars(
            select(Steps).where(
                Steps.end_time > start,
                Steps.start_time < stop,
                Steps.is_quarantined.is_(False),
                valid_source(Steps, "steps"),
            )
        )
    ).all()
    for item in steps:
        left, right = as_utc(item.start_time), as_utc(item.end_time)
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
            count = float(item.count) * (edge - cursor).total_seconds() / duration
            day = local.date()
            rows[day]["steps"] = (rows[day]["steps"] or 0) + count
            if day == end_date:
                step_hours[local.hour] = (step_hours[local.hour] or 0) + count
            cursor = edge

    sleep = (
        await session.scalars(
            select(SleepSession)
            .where(
                SleepSession.end_time >= start,
                SleepSession.end_time < stop,
                SleepSession.is_quarantined.is_(False),
                valid_source(SleepSession, "sleep"),
            )
            .order_by(SleepSession.end_time)
        )
    ).all()
    for day, values in rows.items():
        values["sleep_seconds"] = sleep_total(
            [item for item in sleep if as_utc(item.end_time).astimezone(zone).date() == day]
        )
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
                RawHealthData.source_start_time
                < datetime.combine(end_date + timedelta(days=1), time.min, UTC),
                RawHealthData.is_quarantined.is_(False),
                raw_not_deleted,
            )
            .order_by(RawHealthData.last_seen_at, RawHealthData.google_data_point_id)
        )
    ).all()
    for item in daily:
        payload = payload_for(specs[item.data_type], item.raw_json)
        day = parse_date(payload.get("date"))
        if day in rows:
            key, fields = metrics[item.data_type]
            value = first_number(payload, fields)
            if value is not None:
                rows[day][key] = float(value)

    heart = (
        await session.execute(
            select(HeartRate.timestamp, HeartRate.bpm)
            .where(
                HeartRate.timestamp >= datetime.combine(end_date, time.min, zone).astimezone(UTC),
                HeartRate.timestamp < stop,
                HeartRate.is_quarantined.is_(False),
                valid_source(HeartRate, "heart-rate"),
            )
            .order_by(HeartRate.timestamp)
        )
    ).all()
    buckets: dict[int, list[float]] = {}
    for timestamp, bpm in heart:
        hour = as_utc(timestamp).astimezone(zone).hour
        buckets.setdefault(hour, []).append(float(bpm))

    measurements = []
    latest_weight = await session.scalar(
        select(Weight)
        .where(
            Weight.is_quarantined.is_(False),
            Weight.timestamp < stop,
            valid_source(Weight, "weight"),
        )
        .order_by(Weight.timestamp.desc())
        .limit(1)
    )
    if latest_weight:
        measurements.append(
            {
                "metric": "weight",
                "value": float(latest_weight.kilograms),
                "measured_at": as_utc(latest_weight.timestamp),
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
                raw_not_deleted,
                RawHealthData.is_quarantined.is_(False),
            )
            .order_by(RawHealthData.source_start_time.desc())
            .limit(1)
        )
        if item:
            value = first_number(payload_for(specs[data_type], item.raw_json), fields)
            if value is not None:
                measurements.append(
                    {
                        "metric": metric,
                        "value": float(value) * scale,
                        "measured_at": as_utc(item.source_start_time),
                        "source": "Google",
                    }
                )
    for metric in ("height", "weight", "body_fat", "muscle"):
        item = await session.scalar(
            select(ManualMeasurement)
            .where(ManualMeasurement.metric == metric, ManualMeasurement.measured_at < stop)
            .order_by(ManualMeasurement.measured_at.desc(), ManualMeasurement.created_at.desc())
            .limit(1)
        )
        if item:
            measurements.append(
                {
                    "metric": metric,
                    "value": float(item.value),
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
            "heart_rate": [
                round(sum(buckets[h]) / len(buckets[h]), 1) if h in buckets else None
                for h in range(24)
            ],
        },
        "sleep_method": (
            "Uyanılan güne atanır; uyanık süre çıkarılır. "
            "Örtüşen oturumlar tek sayılır; örtüşmede süre tahminidir."
        ),
    }
