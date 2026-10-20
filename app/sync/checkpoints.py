from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.timeutil import as_utc


def incremental_start(
    last_successful_sync: datetime | None,
    safety_window: timedelta,
    now: datetime | None = None,
) -> datetime:
    now = now or datetime.now(UTC)
    if last_successful_sync is None:
        return now - safety_window
    return min(as_utc(last_successful_sync) - safety_window, now)
