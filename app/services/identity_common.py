"""Shared identity data structures and batched row/alias lookups."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import any_, bindparam, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import RawHealthDataIdentity
from app.models.state import RawHealthData
from app.parsers.identity import RecordIdentity
from app.services.source_chains import SourceState

# Supersession links are followed at most this far (the reconciler links every row of a
# device to one holder, so one step is the norm).
MAX_CHAIN = 16
_CHUNK = 5000
# Spare ids of one record and fingerprint are looked up this many at a time.
SPARE_BLOCK = 8


def _chunks(values: Sequence) -> Iterable[Sequence]:
    for offset in range(0, len(values), _CHUNK):
        yield values[offset : offset + _CHUNK]


def among(session: AsyncSession, column, values: Sequence):
    """``column IN values``; on PostgreSQL one array parameter (``= ANY``), which plans in
    a fraction of the time thousands of separate parameters take."""
    if session.get_bind().dialect.name == "postgresql":
        return column == any_(bindparam(None, list(values), type_=ARRAY(column.type)))
    return column.in_(list(values))


async def aliases(
    session: AsyncSession, data_type: str, keys: Iterable[str]
) -> dict[str, uuid.UUID]:
    found: dict[str, uuid.UUID] = {}
    table = RawHealthDataIdentity
    for chunk in _chunks(sorted(set(keys))):
        rows = await session.execute(
            select(table.identity_key, table.raw_id).where(
                table.data_type == data_type, among(session, table.identity_key, chunk)
            )
        )
        found.update(rows.tuples().all())
    return found


async def rows_by_key(
    session: AsyncSession, data_type: str, google_ids: Iterable[str]
) -> dict[str, uuid.UUID]:
    found: dict[str, uuid.UUID] = {}
    for chunk in _chunks(sorted(set(google_ids))):
        rows = await session.execute(
            select(RawHealthData.google_data_point_id, RawHealthData.id).where(
                RawHealthData.data_type == data_type,
                among(session, RawHealthData.google_data_point_id, chunk),
            )
        )
        found.update(rows.tuples().all())
    return found


@dataclass
class StoredRow:
    raw_id: uuid.UUID
    google_data_point_id: str
    payload_hash: str
    last_seen_at: datetime
    deleted: bool
    quarantined: bool = False
    identity: RecordIdentity | None = None
    reading: str | None = None
    # The stored payload (None when a record of the page has exactly this payload).
    raw_json: object = None
    # Its direct supersession links, any state: (holder raw id, old fingerprint, new
    # fingerprint); None until loaded.
    links: list[tuple[uuid.UUID, str, str]] | None = None

    @property
    def order(self) -> tuple:
        """Most recent first when sorted in reverse: live, then last seen."""
        return (not self.deleted, self.last_seen_at, str(self.raw_id))

    @property
    def state(self) -> tuple[str, bool, bool]:
        """What the page resolved against: payload, deletion mark, quarantine."""
        return (self.payload_hash, self.deleted, self.quarantined)

    def same_source_link(self) -> bool:
        """Superseded by a row of its own fingerprint (always in force)."""
        return any(old == new for _, old, new in self.links or ())


@dataclass
class PlannedLink:
    """A row of a decided device that the page leaves counted beside a row it writes, with the
    same reading: superseded by the written row (``new_google_data_point_id``, created by the
    page or written onto), so the record is counted once, at once."""

    old_google_data_point_id: str
    new_google_data_point_id: str
    reason: str
    old_fingerprint: str
    new_fingerprint: str
    # The holder's payload hash once this page is written.
    holder_payload_hash: str


@dataclass
class Resolution:
    # Per record, in page order: the google_data_point_id to write it under.
    ids: list[str]
    # specific identity key -> google_data_point_id, to alias once the rows exist.
    new_aliases: dict[str, str] = field(default_factory=dict)
    # Existing rows whose state the page relied on - the rows it writes onto, every row that
    # could hide one of them (the targets of their links, any state, transitively) and the
    # rows it links: raw id -> (payload hash, marked deleted, quarantined) as resolved, locked
    # and checked before writing ...
    expected: dict[uuid.UUID, tuple[str, bool, bool]] = field(default_factory=dict)
    # ... and their links (raw id -> the rows it is linked to), checked the same way.
    expected_links: dict[uuid.UUID, frozenset[uuid.UUID]] = field(default_factory=dict)
    # google_data_point_ids of those rows, to lock.
    locked: set[str] = field(default_factory=set)
    # google_data_point_ids of the rows this page creates.
    created: set[str] = field(default_factory=set)
    # Rows of a decided device left counted beside a written row with the same reading.
    links: list[PlannedLink] = field(default_factory=list)
    # Records whose written row holds a row of another fingerprint of their device.
    chained: int = 0
    # Records given a new row because the rows hiding their own showed another value.
    kept_apart: int = 0
    # The page's sources: fingerprint -> (stable class, label, dataSource, newest record
    # time); recorded as sightings late in the transaction (storage), with the response time.
    seen: dict[str, tuple[str, str, object, datetime | None]] = field(default_factory=dict)
    # Keys returned under two fingerprints with two readings: (fingerprint, fingerprint, key).
    evidence: list[tuple[str, str, str]] = field(default_factory=list)
    state: SourceState | None = None


@dataclass
class _Group:
    """The records of one page with one v2 key and one source fingerprint."""

    key: str
    source: str
    identity: RecordIdentity
    indexes: list[int] = field(default_factory=list)
    # The reading, payload hash and record of its last record (what its row holds after
    # the page).
    reading: str | None = None
    digest: str = ""
    record: object = None
