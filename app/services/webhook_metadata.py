"""Validate notification metadata and query coverage without changing raw values."""

import math
import re
from datetime import datetime, timedelta

from sqlalchemy import String

from app.google_health.client import _minute_aligned_range
from app.google_health.data_types import (
    AccessMethod,
    DataTypeSpec,
    QueryWindowError,
    RecordKind,
    query_iso,
)
from app.models.state import WebhookEvent, WebhookNotification
from app.parsers.common import parse_datetime

INDEX_FIELDS = {
    "dataType": "data_type",
    "healthUserId": "health_user_id",
    "operation": "operation",
}
# PostgreSQL text and jsonb hold neither U+0000 nor an unpaired surrogate.
_UNSTORABLE_TEXT = re.compile("[\x00\ud800-\udfff]")
UNREPRESENTABLE = (
    "notification element cannot be stored as jsonb (U+0000, unpaired surrogate or "
    "non-finite number); it is kept only in the signed batch raw_body"
)


class NotificationWindowError(ValueError):
    """A notification's full query coverage is outside the supported date range."""


def index_limit(field: str) -> int:
    """Use the strictest destination so event and notification writes agree."""
    column = INDEX_FIELDS[field]
    limits = []
    for model in (WebhookEvent, WebhookNotification):
        column_type = model.__table__.columns[column].type
        assert isinstance(column_type, String) and column_type.length is not None
        limits.append(column_type.length)
    return min(limits)


def storable_text(value: str) -> bool:
    return _UNSTORABLE_TEXT.search(value) is None


def jsonb_storable(value: object) -> bool:
    """Whether jsonb can hold a parsed value; json.loads also accepts NaN and 1e400.

    Iterative so it sets no depth limit of its own: json.loads and canonical_json (C, one
    recursion level per nesting) already accept depths where a recursive Python walk, at
    two frames per level, raises RecursionError and would fail the ingest.
    """
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if not storable_text(item):
                return False
        elif isinstance(item, float):
            if not math.isfinite(item):
                return False
        elif isinstance(item, dict):
            if not all(storable_text(key) for key in item):
                return False
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return True


def notification_data(item: object) -> dict | None:
    if not isinstance(item, dict):
        return None
    data = item.get("data")
    return data if isinstance(data, dict) else None


def index_metadata(data: dict | None) -> dict[str, str | None]:
    """Project only usable strings; never stringify, truncate or mutate raw data.

    This projection is for persistence only. Acceptance must be decided from the
    original values, including fields that this function replaces with NULL.
    """
    values = data or {}
    result = {}
    for field, column in INDEX_FIELDS.items():
        value = values.get(field)
        result[column] = (
            value
            if isinstance(value, str)
            and 0 < len(value) <= index_limit(field)
            and storable_text(value)
            else None
        )
    return result


def extract_notification(
    item: object, expected_user_id: str | None, supported_data_types: set[str]
) -> tuple[dict, str | None]:
    """Validate unmodified values, retaining a reason for each rejected element."""
    if not isinstance(item, dict):
        return {}, "notification element must be an object"
    data = notification_data(item)
    if data is None:
        return {}, "notification element.data must be an object"
    extracted = {field: data.get(field) for field in (*INDEX_FIELDS, "intervals")}
    for field in INDEX_FIELDS:
        value = extracted[field]
        if not isinstance(value, str) or not value:
            return extracted, f"notification {field} must be a non-empty string"
        limit = index_limit(field)
        if len(value) > limit:
            return extracted, f"notification {field} must be at most {limit} characters"
    if not isinstance(extracted["intervals"], list):
        return extracted, "notification intervals must be an array"
    if expected_user_id is None:
        return extracted, "authorized Google Health user ID is unavailable"
    if extracted["healthUserId"] != expected_user_id:
        return extracted, "notification healthUserId does not match the authorized account"
    if extracted["dataType"] not in supported_data_types:
        return extracted, "notification dataType is not enabled by this mirror"
    return extracted, None


def notification_windows(
    spec: DataTypeSpec, intervals: object
) -> list[tuple[datetime | None, datetime | None]]:
    """Plan and validate every bound before any fetch, for delivery and replay alike.

    Never clamp a covering window or use the default window when date arithmetic
    fails: that would acknowledge a notification without fetching its full coverage.
    Missing physical intervals retain the existing incremental-window behavior.
    """
    start, end = _physical_range(intervals)
    if start is None or end is None:
        return [(None, None)]
    windows: list[tuple[datetime | None, datetime | None]] = []
    try:
        start, end = spec.covering_window(start, end)
        step = timedelta(days=spec.max_window_days) if spec.max_window_days else None
        cursor = start
        while cursor < end:
            # Compare durations first: even a short final piece may be too close
            # to year 9999 to construct cursor + step before taking min(..., end).
            window_end = cursor + step if step is not None and end - cursor > step else end
            if spec.access_method == AccessMethod.rollup:
                # Rollups also widen the upper bound to a complete minute.
                _minute_aligned_range(cursor, window_end)
            if spec.kind == RecordKind.daily:
                # Validate the civil-date ceiling used by the actual Google filter,
                # including its exclusive upper date, before starting any window.
                spec.filter_for(query_iso(cursor), query_iso(window_end))
            windows.append((cursor, window_end))
            cursor = window_end
    except (OverflowError, QueryWindowError) as exc:
        raise NotificationWindowError(
            "notification query window exceeds the representable date range"
        ) from exc
    return windows


def _physical_range(intervals: object) -> tuple[datetime | None, datetime | None]:
    if not isinstance(intervals, list):
        return None, None
    ranges = []
    for item in intervals:
        physical = item.get("physicalTimeInterval", {}) if isinstance(item, dict) else {}
        if not isinstance(physical, dict):
            continue
        start = parse_datetime(physical.get("startTime"))
        end = parse_datetime(physical.get("endTime"))
        if start and end and start < end:
            ranges.append((start, end))
    if not ranges:
        return None, None
    return min(start for start, _ in ranges), max(end for _, end in ranges)
