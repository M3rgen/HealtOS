"""Civil read windows shared by archive validation and interpretation readers."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def bounds(day: date, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    return (
        datetime.combine(day, time.min, zone).astimezone(UTC),
        datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC),
    )


def interpretation_window(
    action: str, first: date, last: date, lookback_days: int
) -> tuple[date, date]:
    lookback = lookback_days if action in {"baseline", "analyze"} else 0
    return first - timedelta(days=lookback), last


def input_bounds(first: date, last: date, timezone: str) -> tuple[datetime, datetime]:
    left, _ = bounds(first, timezone)
    _, stop = bounds(last, timezone)
    # Civil daily summaries are stored at UTC midnight. Keep both clock conventions;
    # these are elapsed UTC days, not additional local calendar days across DST.
    return left - timedelta(days=1), stop + timedelta(days=1)
