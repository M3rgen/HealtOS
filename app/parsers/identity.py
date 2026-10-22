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
*fingerprint* (the hash of the whole ``dataSource``): two records with the same key and
different fingerprints are either the same record seen before and after a change of its
source, or two sources that really are distinct (two devices recording at the same instant).
That is decided per source, never per record, and never by the write path: only the
reconciler (``health-mirror identity-backfill``, app/services/source_chains.py) links two
fingerprints into one device's chain, from their whole sighting history.

``device.formFactor`` and ``device.manufacturer`` are *optional device fields*: Google may add one
to a source's records that it always sent without (it began sending ``formFactor`` for a band
known until then by its name alone, sometimes without the name). They stay in the key - every
key and row id stored is kept as it was - but the *match key* (``RecordIdentity.match``: the key
without them; the key itself when the source has none) is what tells which rows of two
fingerprints are one record. Two fingerprints are *kindred* (``kindred``: may be forms of one
device) when they are of one stable class (a rename, as always), or when their ``dataSource`` is
the same but for device fields one has and the other lacks - an optional device field, and with
it perhaps the ``displayName`` - so a form with ``formFactor`` added is compared like a rename,
while a phone and a watch, or two devices with two names, never are. Kindred is not transitive
(a source without ``formFactor`` is kindred to a phone and to a watch of its name): one device
only ever joins fingerprints whose optional device fields are *compatible* two by two
(``compatible``: none with two values), so a phone and a watch are never one device, through
whatever forms (app/services/source_chains.py).

``specific_key`` is the key plus the fingerprint. A record stored for the first time gets the
id ``new_record_id(specific_key)`` (``synthetic:v2s:<hash>``): every fingerprint has its own
row, so the payload of one source is never written onto another source's row by accident.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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


# Device fields of the stable source that Google may add to (or drop from) a source's records
# while the source stays the same.
OPTIONAL_DEVICE_FIELDS = ("formFactor", "manufacturer")


def stable_source(data_source: object, *, optional: bool = True) -> object:
    """The ``dataSource`` fields that identify a source across renames; ``optional=False``:
    without the optional device fields (as ``None``, so a source that has none gives the same)."""
    if not isinstance(data_source, dict):
        return data_source
    device = _object(data_source.get("device"))
    application = _object(data_source.get("application"))
    return {
        "platform": data_source.get("platform"),
        "recordingMethod": data_source.get("recordingMethod"),
        "formFactor": device.get("formFactor") if optional else None,
        "manufacturer": device.get("manufacturer") if optional else None,
        "package": stable_package(application.get("packageName")),
    }


def optional_fields(data_source: object) -> tuple:
    """The optional device fields of a source (``None`` where it has none)."""
    device = _object(_object(data_source).get("device"))
    return tuple(device.get(name) for name in OPTIONAL_DEVICE_FIELDS)


def compatible(left: object, right: object) -> bool:
    """Two ``dataSource`` values have no optional device field with two values: they may be
    forms of one device (a device is never a phone and a watch)."""
    return all(
        one == other or one is None or other is None
        for one, other in zip(optional_fields(left), optional_fields(right), strict=True)
    )


# Device fields a form of a source may have and another form of it lack.
_ADDED_FIELDS = frozenset({*OPTIONAL_DEVICE_FIELDS, "displayName"})


def kindred(left: object, right: object) -> bool:
    """Two ``dataSource`` values may be forms of one device: one stable class (a rename, a
    dropped package suffix), or the same ``dataSource`` but for device fields one has and the
    other lacks, among the optional device fields and ``displayName`` (Google added
    ``formFactor``, perhaps dropping the name). Two values of one field never are."""
    if stable_source(left) == stable_source(right):
        return True
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    if {name: value for name, value in left.items() if name != "device"} != {
        name: value for name, value in right.items() if name != "device"
    }:
        return False
    one, other = _object(left.get("device")), _object(right.get("device"))
    for name in one.keys() | other.keys():
        if name in one and name in other:
            if one[name] != other[name]:
                return False
        elif name not in _ADDED_FIELDS:
            return False
    return True


def source_family(data_source: object) -> str:
    """Hash of the stable source fields without the optional ones: fingerprints that may share
    a record (their match key)."""
    return payload_hash(stable_source(data_source, optional=False))


def stable_class(data_source: object) -> str:
    """Hash of the stable source fields: fingerprints of one class can share a v2 key."""
    return payload_hash(stable_source(data_source))


def source_label(data_source: object) -> str:
    """A short readable name of a source for reports: platform, device or package name."""
    if not isinstance(data_source, dict):
        return "(no dataSource)"
    device = _object(data_source.get("device"))
    application = _object(data_source.get("application"))
    parts = [
        data_source.get("platform"),
        device.get("displayName"),
        application.get("packageName"),
        data_source.get("recordingMethod"),
    ]
    return " / ".join(str(part) for part in parts if part)[:255] or "(empty dataSource)"


def instant(value: datetime | None) -> str | None:
    return as_utc(value).isoformat() if value is not None else None


def source_fingerprint(data_source: object) -> str:
    return payload_hash(data_source)


@dataclass(frozen=True)
class RecordIdentity:
    # data type + physical time + discriminators + stable source fields.
    key: str
    # ``key`` plus the fingerprint: the record of exactly this source (its row's id).
    specific_key: str
    # Hash of the whole dataSource (renames, package suffixes and unknown fields).
    source: str
    # Growing intervals: the end instant, which the key leaves out.
    shape: str | None
    # The physical start (``temporal``), where stored rows of the same identity are found.
    start: datetime | None
    # Hash of the stable source fields (the part of ``source`` that is in ``key``).
    stable: str
    # ``key`` without the optional device fields (``key`` itself when the source has none): the
    # rows of two kindred fingerprints with one match key hold one record if they are one
    # device. Never stored or used for an id.
    match: str = ""
    # The record's dataSource (for ``kindred``; not part of the identity's value).
    data_source: object = field(default=None, compare=False, repr=False)

    def kindred(self, other: RecordIdentity) -> bool:
        """``other`` (of the same match key) may be this record of a form of its source."""
        return kindred(self.data_source, other.data_source)

    def compatible(self, other: RecordIdentity) -> bool:
        """``other`` (of the same match key) may be this record of the same device (no optional
        device field with two values)."""
        return compatible(self.data_source, other.data_source)

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
    digest = payload_hash(parts)
    loose = stable_source(data_source, optional=False)
    match = digest if loose == parts["source"] else payload_hash({**parts, "source": loose})
    shape = instant(end) if growing else None
    # The time leads the key only for locality: aliases of one period sit together in the
    # index, so the backfill (in time order) and each page's lookups touch few index pages.
    moment = start if start is not None else end
    assert moment is not None
    stamp = as_utc(moment).strftime("%Y%m%dT%H%M%S")
    source = source_fingerprint(data_source)
    key = f"{KEY_PREFIX}:{stamp}:{digest}"
    return RecordIdentity(
        key=key,
        specific_key=specific_key(key, source),
        source=source,
        shape=shape,
        start=start,
        stable=payload_hash(parts["source"]),
        match=f"{KEY_PREFIX}:{stamp}:{match}",
        data_source=data_source,
    )


def pair_key(left: RecordIdentity, right: RecordIdentity) -> str:
    """The key two fingerprints' rows of one record are compared under (evidence of two
    devices, agreement): their v2 key when they share it (as before optional device fields
    were compared), else their match key."""
    return left.key if left.key == right.key else left.match


def key_under(spec: DataTypeSpec, record: dict, data_source: object) -> str | None:
    """The v2 key of ``record`` as the source ``data_source`` would send it (another form of
    its device: the key differs when their optional device fields do)."""
    identity = record_identity(spec, {**record, "dataSource": data_source})
    return identity.key if identity is not None else None


def specific_key(key: str, source: str) -> str:
    """The key of the record ``key`` as sent by the source ``source`` (a fingerprint).

    Without the shape: a growing interval of one source stays one row while its end grows."""
    _, stamp, digest = key.split(":")
    return f"{SPECIFIC_PREFIX}:{stamp}:{payload_hash({'key': digest, 'source': source})}"


def new_record_id(identity_key: str) -> str:
    """The ``google_data_point_id`` of a record first stored under ``identity_key`` (its
    ``specific_key``): ``synthetic:v2s:<hash>``."""
    prefix, _, digest = identity_key.split(":")
    return f"synthetic:{prefix}:{digest}"


def spare_record_id(identity: RecordIdentity, attempt: int) -> str:
    """Another id of this source's record, for when ``new_record_id`` is taken by a row that
    holds another source's payload now (a chain wrote it there)."""
    digest = payload_hash({"key": identity.specific_key, "n": attempt})
    return f"synthetic:{SPECIFIC_PREFIX}:{digest}"


def reading(spec: DataTypeSpec, record: object) -> str | None:
    """Hash of what a record measured: its payload without the source and with its time as
    parsed (civil times and offsets do not count; a growing interval counts by its start).

    Two fingerprints of one device give the same reading for the same record. The reconciler
    only merges rows of *different* fingerprints whose readings are equal, and a decided chain
    only writes onto a row whose reading equals the incoming one: a source wrongly taken for a
    renamed device can never hide a value of its own."""
    if not isinstance(record, dict):
        return None
    payload = {
        name: value
        for name, value in payload_for(spec, record).items()
        if name not in ("interval", "sampleTime", "date")
    }
    start, end, _ = temporal(spec, record)
    payload["@start"] = instant(start)
    if spec.name not in GROWING_INTERVAL_TYPES:
        payload["@end"] = instant(end)
    return payload_hash(payload)


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
