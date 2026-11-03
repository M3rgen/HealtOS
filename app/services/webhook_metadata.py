"""Validate notification metadata and query coverage without changing raw values."""

import json
from datetime import datetime, timedelta

from sqlalchemy import String

from app.google_health.client import _minute_aligned_range
from app.google_health.data_types import (
    AccessMethod,
    DataTypeSpec,
    QueryWindowError,
    query_iso,
)
from app.google_health.jsonb import UNPARSED_BODY as UNPARSED_BODY
from app.google_health.jsonb import jsonb_storable as jsonb_storable
from app.google_health.jsonb import storable_text as storable_text
from app.models.state import WebhookEvent, WebhookNotification
from app.parsers.common import parse_datetime

INDEX_FIELDS = {
    "dataType": "data_type",
    "healthUserId": "health_user_id",
    "operation": "operation",
}
UNREPRESENTABLE = (
    "notification element cannot be stored as jsonb (U+0000, unpaired surrogate or "
    "non-finite number); it is kept only in the signed batch raw_body"
)
UNPARSEABLE = (
    "notification body could not be parsed as JSON; it is kept only in the signed batch raw_body"
)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject ambiguous objects before any notification metadata can be projected.

    The JSON decoder calls this for each object with escapes already decoded.
    Scope the seen keys to that object: siblings in a batch may share keys.
    Equal values still discard signed evidence and must also be rejected.
    """
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def parse_webhook_body(raw: bytes) -> object:
    """Share the delivery and body-only replay decision, including ambiguous JSON.

    Reject the entire body when any object repeats a key; element boundaries and
    metadata must never come from a last-value-wins interpretation. The caller
    verifies the signature before archiving the original bytes as UNPARSED_BODY.
    """
    try:
        return json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError, OverflowError, MemoryError):
        # Includes bad encodings, integer limits and excessive nesting. Do not retain
        # parser exception text: it may contain private values from the signed body.
        return UNPARSED_BODY


class NotificationWindowError(ValueError):
    """A notification's full query coverage is invalid or unrepresentable."""


def index_limit(field: str) -> int:
    """Use the strictest destination so event and notification writes agree."""
    column = INDEX_FIELDS[field]
    limits = []
    for model in (WebhookEvent, WebhookNotification):
        column_type = model.__table__.columns[column].type
        assert isinstance(column_type, String) and column_type.length is not None
        limits.append(column_type.length)
    return min(limits)


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
    Missing physical intervals retain the existing incremental-window behavior;
    explicitly supplied physical intervals must all be valid, even alongside valid ones.
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
            else:
                # Validate second and civil-date ceilings before any fetch, including
                # a final subsecond bound whose next second is unrepresentable.
                spec.filter_for(query_iso(cursor), query_iso(window_end, upper=True))
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
        # Absence alone permits the default window. A supplied null, empty object
        # or malformed bound must never turn into a partial or default fetch.
        if not isinstance(item, dict) or "physicalTimeInterval" not in item:
            continue
        ranges.append(_physical_interval(item["physicalTimeInterval"]))
    if not ranges:
        return None, None
    return min(start for start, _ in ranges), max(end for _, end in ranges)


def _physical_interval(physical: object) -> tuple[datetime, datetime]:
    """Validate a supplied interval without including any raw values in the error.

    parse_datetime returns None for missing, non-string and malformed bounds,
    including local timestamps whose UTC conversion overflows. Keep that parser's
    contract; only notification coverage requires both bounds and strict ordering.
    """
    if isinstance(physical, dict):
        start = parse_datetime(physical.get("startTime"))
        end = parse_datetime(physical.get("endTime"))
        if start is not None and end is not None and start < end:
            return start, end
    raise NotificationWindowError(
        "notification physicalTimeInterval must have valid startTime before endTime"
    )
