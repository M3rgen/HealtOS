from __future__ import annotations

from datetime import UTC, datetime, timedelta


def incremental_start(
    last_successful_sync: datetime | None,
    safety_window: timedelta,
    now: datetime | None = None,
) -> datetime:
    now = now or datetime.now(UTC)
    if last_successful_sync is None:
        return now - safety_window
    if last_successful_sync.tzinfo is None:
        last_successful_sync = last_successful_sync.replace(tzinfo=UTC)
    return min(last_successful_sync - safety_window, now)
