"""Read raw coverage values in dashboard units, without changing the archive.

Field selection precedes validation: a nonfinite preferred field must not expose
a lower-priority field. A rejected row is yielded once with no day so the shared
coverage reader can count it as invalid_shape, separately from retrieval evidence.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.google_health.data_types import supported_data_types
from app.models.eligibility import raw_counted
from app.models.state import RawHealthData
from app.parsers.common import first_number, parse_date, payload_for
from app.services.finite_math import finite_number, finite_scale

if TYPE_CHECKING:
    from app.services.coverage import Metric, Window


async def raw_value_days(
    session: AsyncSession, metric: Metric, window: Window
) -> AsyncIterator[tuple[object, date | None]]:
    """Yield each eligible source and its value day; None means an invalid row.

    Daily values use civil dates and daily query bounds. Samples use the local
    day of the stored source timestamp, including a correction's current time.
    Height keeps its source Decimal through conversion from millimeters to cm:
    a value outside float range may still be representable in the final unit.
    True zero remains a value; overflow, underflow to zero and nonfinite values
    cannot establish presence, even when the source date is well formed.
    """
    spec = supported_data_types()[metric.data_type or ""]
    start, stop = window.start, window.stop
    if metric.read == "raw_daily":
        start = datetime.combine(window.first, time.min, UTC)
        stop = datetime.combine(window.end + timedelta(days=1), time.min, UTC)
    rows = await session.execute(
        select(RawHealthData.raw_json, RawHealthData.source_start_time).where(
            RawHealthData.data_type == metric.data_type,
            RawHealthData.source_start_time >= start,
            RawHealthData.source_start_time < stop,
            raw_counted(data_types=(metric.data_type,)),
        )
    )
    for record, started in rows:
        payload = payload_for(spec, record)
        day = (
            parse_date(payload.get("date")) if metric.read == "raw_daily" else window.local(started)
        )
        if day is not None and window.first <= day <= window.end:
            supplied = first_number(payload, metric.fields)
            value = (
                finite_scale(supplied, 0.1)
                if metric.key == "height" and supplied is not None
                else finite_number(supplied)
            )
            if value is not None:
                yield record.get("dataSource"), day
                continue
        yield record.get("dataSource"), None
