"""Stable identity (v2) for records that carry no Google id.

Most data types arrive without a ``name``; ``source_id`` (v1) then hashes the raw interval
object and the whole ``dataSource``. Both change while the record stays the same: Google
renamed a device (``displayName``), Apple Health dropped the ``.<UUID>`` suffix of its package
name, Google added civil times and UTC offsets inside ``interval``, and an in-progress
sedentary period keeps its start while its end grows. Every such change made v1 a new id, so
the same record was archived twice.

The v2 key hashes only what identifies the record physically:

* the data type;
* the physical UTC start and end (intervals) or instant (samples) as parsed by ``temporal``
  (never the raw interval object, so civil times and offsets do not count); for
  ``GROWING_INTERVAL_TYPES`` the start alone;
* the zone/level discriminators (``DISCRIMINATORS``);
* the stable source fields: ``platform``, ``recordingMethod``, ``device.formFactor``,
  ``device.manufacturer`` and ``application.packageName`` without a trailing ``.<UUID>``.

``displayName`` and every other ``dataSource`` field are left out of the key but kept in the
*fingerprint*: two records with the same key and different fingerprints are either the same
record seen before and after a change, or two sources that really are distinct (two devices
recording at the same instant). Records seen together, in one batch or while the other is
still being returned, are distinct; they fall back to ``specific_key``, which includes the
fingerprint (see app/services/storage.py and app/services/identity_backfill.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.timeutil import as_utc
from app.google_health.data_types import DataTypeSpec
from app.parsers.common import google_name, payload_for, payload_hash, temporal

# Intervals that Google returns while they are still in progress: the start is fixed, the
# end grows with every fetch (sedentary-period: every 15 minutes). Checked against the
# archive: no other interval type has two records with the same start, source and
# discriminators but different ends (sleep and exercise carry Google ids).
GROWING_INTERVAL_TYPES = frozenset({"sedentary-period"})
# Payload fields that tell records of the same interval apart (v1's list plus
# time-in-heart-rate-zone's zone field, which v1 missed).
DISCRIMINATORS = (
    "heartRateZone",
    "heartRateZoneType",
    "activityLevelType",
    "measurementLocation",
    "measurementMethod",
    "swimStrokeType",
)
_UUID_SUFFIX = re.compile(
    r"\.[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
KEY_PREFIX = "v2"
SPECIFIC_PREFIX = "v2s"


def stable_package(name: object) -> object:
    return _UUID_SUFFIX.sub("", name) if isinstance(name, str) else name


def _object(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def stable_source(data_source: object) -> object:
    """The ``dataSource`` fields that identify a source across renames."""
    if not isinstance(data_source, dict):
        return data_source
    device = _object(data_source.get("device"))
    application = _object(data_source.get("application"))
    return {
        "platform": data_source.get("platform"),
        "recordingMethod": data_source.get("recordingMethod"),
        "formFactor": device.get("formFactor"),
        "manufacturer": device.get("manufacturer"),
        "package": stable_package(application.get("packageName")),
    }


def instant(value: datetime | None) -> str | None:
    return as_utc(value).isoformat() if value is not None else None


def source_fingerprint(data_source: object) -> str:
    return payload_hash(data_source)


@dataclass(frozen=True)
class RecordIdentity:
    # data type + physical time + discriminators + stable source fields.
    key: str
    # ``key`` plus the fingerprint: for sources that must be kept apart.
    specific_key: str
    # Hash of the whole dataSource (renames, package suffixes and unknown fields).
    source: str
    # Growing intervals: the end instant, which the key leaves out.
    shape: str | None
    # The physical start (``temporal``), where stored rows of the same identity are found.
    start: datetime | None

    @property
    def fingerprint(self) -> tuple[str, str | None]:
        return self.source, self.shape


def record_identity(spec: DataTypeSpec, record: object) -> RecordIdentity | None:
    """The v2 identity of a record without a Google id; ``None`` when it has one (or no time).

    A record without any parsable time cannot be identified physically; it keeps its v1 id.
    """
    if not isinstance(record, dict) or google_name(record) is not None:
        return None
    start, end, _ = temporal(spec, record)
    growing = spec.name in GROWING_INTERVAL_TYPES
    if start is None and (growing or end is None):
        return None
    parts: dict[str, Any] = {"identity": 2, "dataType": spec.name, "start": instant(start)}
    if not growing:
        parts["end"] = instant(end)
    payload = payload_for(spec, record)
    for name in DISCRIMINATORS:
        if name in payload:
            parts[name] = payload[name]
    data_source = record.get("dataSource")
    parts["source"] = stable_source(data_source)
    key = payload_hash(parts)
    shape = instant(end) if growing else None
    # The time leads the key only for locality: aliases of one period sit together in the
    # index, so the backfill (in time order) and each page's lookups touch few index pages.
    moment = start if start is not None else end
    assert moment is not None
    stamp = as_utc(moment).strftime("%Y%m%dT%H%M%S")
    specific = payload_hash({"key": key, "dataSource": data_source, "shape": shape})
    return RecordIdentity(
        key=f"{KEY_PREFIX}:{stamp}:{key}",
        specific_key=f"{SPECIFIC_PREFIX}:{stamp}:{specific}",
        source=source_fingerprint(data_source),
        shape=shape,
        start=start,
    )


def new_record_id(identity_key: str) -> str:
    """The ``google_data_point_id`` of a record first stored under ``identity_key``:
    ``synthetic:v2:<hash>`` (``synthetic:v2s:<hash>`` for a source kept apart)."""
    prefix, _, digest = identity_key.split(":")
    return f"synthetic:{prefix}:{digest}"


def _times(spec: DataTypeSpec, record: dict) -> tuple:
    payload = payload_for(spec, record)
    return (
        payload.get("interval"),
        payload.get("sampleTime"),
        payload.get("date"),
        record.get("startTime"),
        record.get("endTime"),
    )


def supersession_reason(spec: DataTypeSpec, old: dict, new: dict) -> str:
    """Why ``old`` and ``new`` (same v2 key) were two rows: the first difference found."""
    if spec.name in GROWING_INTERVAL_TYPES and temporal(spec, old)[1] != temporal(spec, new)[1]:
        return "growing-interval"
    old_source, new_source = old.get("dataSource"), new.get("dataSource")
    if old_source != new_source:
        old_device = _object(_object(old_source).get("device"))
        new_device = _object(_object(new_source).get("device"))
        if old_device.get("displayName") != new_device.get("displayName"):
            return "display-name-change"
        old_package = _object(_object(old_source).get("application")).get("packageName")
        new_package = _object(_object(new_source).get("application")).get("packageName")
        if old_package != new_package:
            return "package-suffix-change"
        return "source-change"
    if _times(spec, old) != _times(spec, new):
        return "interval-shape-change"
    return "duplicate"
