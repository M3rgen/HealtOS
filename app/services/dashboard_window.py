"""Representable civil windows for dashboard readers, checked before querying data."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


class DateWindowError(ValueError):
    """A civil window this timezone cannot represent; the API layer answers 422."""


@contextmanager
def checked_dates() -> Iterator[None]:
    """Translate date arithmetic/conversion overflow without exposing exception details."""
    try:
        yield
    except OverflowError as exc:
        raise DateWindowError("Seçilen tarih aralığı bu saat diliminde hesaplanamıyor.") from exc


@dataclass(frozen=True)
class DashboardWindow:
    first: date
    following: date
    start: datetime
    stop: datetime


def civil_window(first: date, following: date, zone: ZoneInfo) -> DashboardWindow:
    """Keep complete half-open civil boundaries; never clamp dates or UTC offsets."""
    with checked_dates():
        return DashboardWindow(
            first,
            following,
            datetime.combine(first, time.min, zone).astimezone(UTC),
            datetime.combine(following, time.min, zone).astimezone(UTC),
        )


def daily_window(end_date: date, days: int, zone: ZoneInfo) -> DashboardWindow:
    with checked_dates():
        first = end_date - timedelta(days=days - 1)
        following = end_date + timedelta(days=1)
    return civil_window(first, following, zone)


def monthly_window(month: date, zone: ZoneInfo) -> DashboardWindow:
    with checked_dates():
        first = month.replace(day=1)
        following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return civil_window(first, following, zone)
