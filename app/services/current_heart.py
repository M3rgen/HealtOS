"""Bounded heart-rate chart reads from the current source payload."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.google_health.data_types import REGISTRY
from app.models.health import HeartRate
from app.models.state import RawHealthData
from app.parsers.common import first_number, payload_for, temporal
from app.services.finite_math import finite_mean, finite_number


def current_heart_rate(record: dict) -> tuple[datetime, float] | None:
    """Validate both time and value without borrowing retained normalized columns.

    Storage intentionally retains HeartRate when a correction cannot normalize. It
    remains archive evidence, rather than a fallback measurement for a newer payload.
    Use the ingestion parser's payload keys, temporal forms and numeric field aliases;
    zero is a supplied value. Identity, quarantine and deletion belong to the query.
    """
    spec = REGISTRY["heart-rate"]
    timestamp, _, _ = temporal(spec, record)
    bpm = finite_number(first_number(payload_for(spec, record), ("beatsPerMinute", "bpm", "value")))
    if timestamp is None or bpm is None:
        return None
    return timestamp, bpm


async def heart_rate_hours(
    session: AsyncSession, day: date, zone: ZoneInfo, source_filter
) -> list[float | None]:
    """Read only current source-time candidates in the requested civil day.

    The caller supplies its eligibility predicate, including include_source_deleted's
    identity rules. An alias keeps its raw-table probes separate from this join. No
    normalized dates select candidates, and neither archive representation is mutated.
    """
    start = datetime.combine(day, time.min, zone).astimezone(UTC)
    stop = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC)
    raw = aliased(RawHealthData)
    records = await session.scalars(
        select(raw.raw_json)
        .select_from(HeartRate)
        .join(
            raw,
            and_(
                raw.data_type == "heart-rate",
                raw.google_data_point_id == HeartRate.google_data_point_id,
            ),
        )
        .where(
            raw.source_start_time >= start,
            raw.source_start_time < stop,
            HeartRate.is_quarantined.is_(False),
            source_filter,
        )
        .order_by(raw.source_start_time)
    )
    buckets: dict[int, list[float]] = {}
    for record in records:
        measurement = current_heart_rate(record)
        if measurement is None:
            continue
        timestamp, bpm = measurement
        hour = timestamp.astimezone(zone).hour
        buckets.setdefault(hour, []).append(bpm)
    means = [finite_mean(buckets.get(hour, [])) for hour in range(24)]
    return [round(value, 1) if value is not None else None for value in means]
