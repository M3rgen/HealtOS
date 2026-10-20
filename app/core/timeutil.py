"""Time-zone helpers shared by the sync, service and API layers."""

from datetime import UTC, datetime
from typing import overload


@overload
def as_utc(value: datetime) -> datetime: ...


@overload
def as_utc(value: None) -> None: ...


def as_utc(value: datetime | None) -> datetime | None:
    """Return ``value`` as an aware UTC datetime.

    SQLite (tests) hands timestamps back naive; they are stored as UTC, so a naive value is
    labelled UTC. Aware values are converted to UTC (the instant never changes). ``None``
    passes through so optional columns need no extra branch.
    """
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
