"""Resolve records without a Google id to the raw row that holds them (v2 identities).

``resolve_ids`` decides, for one page of records, under which ``google_data_point_id`` each
is written:

1. A record with a Google ``name`` keeps it.
2. Its specific key (v2 + fingerprint) has an alias: that row.
3. Its v2 key has an alias: that row, unless the row holds a *distinct* source (below).
4. No alias yet: a stored row with the same v2 key, found among the rows with the same
   ``source_start_time`` (indexed) or the record's v1 id. Preferred: the row under its v1 id,
   then a row with the same fingerprint, then the newest live row. An alias is added.
5. Otherwise a new row ``synthetic:v2:<hash>`` with an alias.

Step 4 makes a rename merge correctly even for rows archived before v2 and before
``health-mirror identity-backfill`` gave them aliases. Aliases and rows reached through a
supersession link are followed to the row that holds the record now. A raw row's own key
never changes: a returning record is written onto the row, so a changed payload goes through
the usual UPSERT and the version trigger keeps the old payload.

Distinct sources. The v2 key leaves out ``displayName`` (and every other ``dataSource``
field except the stable ones) and, for growing intervals, the end. Two records with the same
key but a different fingerprint are kept apart when they are seen together:

* in the same page (``contested`` keys): each goes to the row with its exact fingerprint;
* the stored row has another ``dataSource`` and was last seen less than
  ``CONCURRENT_SIGHTING`` ago (a page of the same fetch, or two devices synced together).

Such a record falls back to its specific key (a stored row with its exact fingerprint, else a
new ``synthetic:v2s:<hash>`` row). A rename is a change seen *after* the old form stopped
appearing, so it merges: on 19 Oct the old device name was last seen six hours before the new
one first appeared.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import any_, bindparam, func, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import as_utc
from app.google_health.data_types import DataTypeSpec
from app.models.identity import RawHealthDataIdentity, RawHealthDataSupersession
from app.models.state import RawHealthData
from app.parsers.common import source_id
from app.parsers.identity import RecordIdentity, new_record_id, record_identity

# A stored row seen this recently, with another dataSource, is a concurrent source.
CONCURRENT_SIGHTING = timedelta(minutes=2)
# Supersession links are followed at most this far (they never form a cycle: a row that is
# itself superseded is never linked to).
MAX_CHAIN = 16
_CHUNK = 5000


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


async def successors(session: AsyncSession, raw_ids: Iterable[uuid.UUID]) -> dict:
    """raw id -> the row that holds its record now (itself unless superseded)."""
    result = {raw_id: raw_id for raw_id in raw_ids}
    frontier = sorted(set(result.values()))
    for _ in range(MAX_CHAIN):
        step: dict[uuid.UUID, uuid.UUID] = {}
        for chunk in _chunks(frontier):
            rows = await session.execute(
                select(
                    RawHealthDataSupersession.old_raw_id, RawHealthDataSupersession.new_raw_id
                ).where(among(session, RawHealthDataSupersession.old_raw_id, chunk))
            )
            step.update(rows.tuples().all())
        if not step:
            break
        for start, current in result.items():
            if current in step:
                result[start] = step[current]
        frontier = sorted(set(step.values()))
    return result


@dataclass(frozen=True)
class StoredRow:
    raw_id: uuid.UUID
    google_data_point_id: str
    payload_hash: str
    last_seen_at: datetime
    deleted: bool

    @property
    def order(self) -> tuple:
        """Newest first when sorted in reverse: live, then last seen."""
        return (not self.deleted, self.last_seen_at, str(self.raw_id))


_ROW = (
    RawHealthData.id,
    RawHealthData.google_data_point_id,
    RawHealthData.payload_hash,
    RawHealthData.last_seen_at,
    RawHealthData.source_deleted_at,
)


def _stored(row) -> StoredRow:
    raw_id, google_id, digest, seen, deleted = row
    return StoredRow(raw_id, google_id, digest, as_utc(seen), deleted is not None)


async def stored_rows(session: AsyncSession, raw_ids: Iterable[uuid.UUID]) -> dict:
    found: dict[uuid.UUID, StoredRow] = {}
    for chunk in _chunks(sorted(set(raw_ids))):
        rows = await session.execute(select(*_ROW).where(among(session, RawHealthData.id, chunk)))
        for row in rows.tuples():
            found[row[0]] = _stored(row)
    return found


async def stored_identities(
    session: AsyncSession, spec: DataTypeSpec, raw_ids: Iterable[uuid.UUID]
) -> dict:
    found: dict[uuid.UUID, RecordIdentity | None] = {}
    for chunk in _chunks(sorted(set(raw_ids))):
        rows = await session.execute(
            select(RawHealthData.id, RawHealthData.raw_json).where(
                among(session, RawHealthData.id, chunk)
            )
        )
        for raw_id, raw_json in rows.tuples():
            found[raw_id] = record_identity(spec, raw_json)
    return found


async def rows_like(
    session: AsyncSession, spec: DataTypeSpec, starts: Iterable[datetime], google_ids: Iterable[str]
) -> dict[uuid.UUID, tuple[StoredRow, RecordIdentity | None]]:
    """Stored rows of ``spec`` with one of these start times or keys, with their identity."""
    found: dict[uuid.UUID, tuple[StoredRow, RecordIdentity | None]] = {}
    columns = (*_ROW, RawHealthData.raw_json)
    conditions = []
    for chunk in _chunks(sorted(set(starts))):
        conditions.append(among(session, RawHealthData.source_start_time, chunk))
    for chunk in _chunks(sorted(set(google_ids))):
        conditions.append(among(session, RawHealthData.google_data_point_id, chunk))
    for condition in conditions:
        rows = await session.execute(
            select(*columns).where(RawHealthData.data_type == spec.name, condition)
        )
        for row in rows.tuples():
            found[row[0]] = (_stored(row[:5]), record_identity(spec, row[5]))
    return found


@dataclass
class Resolution:
    # Per record, in page order: the google_data_point_id to write it under.
    ids: list[str]
    # identity key -> google_data_point_id, to alias once the rows exist.
    new_aliases: dict[str, str]


class _Rows:
    """Stored rows met while resolving one page, and the rows holding them now."""

    def __init__(self, session: AsyncSession, spec: DataTypeSpec) -> None:
        self.session, self.spec = session, spec
        self.info: dict[uuid.UUID, StoredRow] = {}
        self.identity: dict[uuid.UUID, RecordIdentity | None] = {}
        self.current: dict[uuid.UUID, uuid.UUID] = {}
        self.by_key: dict[str, set[uuid.UUID]] = defaultdict(set)
        self.by_specific: dict[str, set[uuid.UUID]] = defaultdict(set)
        self.by_google_id: dict[str, uuid.UUID] = {}

    async def follow(self, raw_ids: Iterable[uuid.UUID]) -> None:
        todo = set(raw_ids) - set(self.current)
        if todo:
            self.current.update(await successors(self.session, todo))
        missing = set(self.current.values()) - set(self.info)
        if missing:
            self.info.update(await stored_rows(self.session, missing))

    async def find(self, starts: Iterable[datetime], google_ids: Iterable[str]) -> None:
        found = await rows_like(self.session, self.spec, starts, google_ids)
        for raw_id, (row, identity) in found.items():
            self.info[raw_id] = row
            self.identity[raw_id] = identity
            self.by_google_id[row.google_data_point_id] = raw_id
            if identity is not None:
                self.by_key[identity.key].add(raw_id)
                self.by_specific[identity.specific_key].add(raw_id)
        await self.follow(found)
        await self.load_identities(self.current[raw_id] for raw_id in found)

    async def load_identities(self, raw_ids: Iterable[uuid.UUID]) -> None:
        missing = set(raw_ids) - set(self.identity)
        if missing:
            self.identity.update(await stored_identities(self.session, self.spec, missing))

    def holder(self, raw_id: uuid.UUID) -> StoredRow:
        return self.info[self.current[raw_id]]

    def newest(self, raw_ids: Iterable[uuid.UUID]) -> StoredRow | None:
        holders = {self.current[raw_id] for raw_id in raw_ids}
        return max(
            (self.info[raw_id] for raw_id in holders), key=lambda row: row.order, default=None
        )


async def resolve_ids(
    session: AsyncSession,
    spec: DataTypeSpec,
    records: Sequence[object],
    digests: Sequence[str],
    now: datetime,
) -> Resolution:
    """``digests``: each record's ``payload_hash``, in the same order."""
    ids = [source_id(spec, record) for record in records]
    resolution = Resolution(ids, {})
    todo: list[tuple[int, RecordIdentity, datetime | None]] = []
    for index, record in enumerate(records):
        identity = record_identity(spec, record)
        if identity is not None:
            todo.append((index, identity, identity.start))
    if not todo:
        return resolution

    fingerprints: dict[str, set] = defaultdict(set)
    for _, identity, _ in todo:
        fingerprints[identity.key].add(identity.fingerprint)
    contested = {key for key, found in fingerprints.items() if len(found) > 1}
    keys = {key for _, identity, _ in todo for key in (identity.key, identity.specific_key)}
    alias = await aliases(session, spec.name, keys)
    rows = _Rows(session, spec)
    await rows.follow(alias.values())
    # Without an alias, look for a stored row of the same identity.
    unaliased = [
        item for item in todo if item[1].key not in alias and item[1].specific_key not in alias
    ]
    if unaliased:
        await rows.find(
            (start for _, _, start in unaliased if start is not None),
            (ids[index] for index, _, _ in unaliased),
        )
    # Fingerprints of stored rows an incoming record might be a distinct source next to.
    compare = set()
    for index, identity, _ in todo:
        if identity.specific_key not in alias and identity.key in alias:
            holder = rows.holder(alias[identity.key])
            if identity.key in contested or holder.payload_hash != digests[index]:
                compare.add(holder.raw_id)
    await rows.load_identities(compare)

    def distinct(identity: RecordIdentity, holder: StoredRow) -> bool:
        """``holder`` keeps another source than the incoming record (see docstring)."""
        other = rows.identity.get(holder.raw_id)
        if other is None or other.fingerprint == identity.fingerprint:
            return False
        if identity.key in contested:
            return True
        if other.source == identity.source:
            return False  # only a growing end changed
        return holder.last_seen_at >= now - CONCURRENT_SIGHTING

    specific: list[tuple[int, RecordIdentity, datetime | None, uuid.UUID | None]] = []
    for index, identity, start in todo:
        if identity.specific_key in alias:
            ids[index] = rows.holder(alias[identity.specific_key]).google_data_point_id
            continue
        if identity.key in alias:
            holder = rows.holder(alias[identity.key])
            if distinct(identity, holder):
                specific.append((index, identity, start, holder.raw_id))
            else:
                ids[index] = holder.google_data_point_id
            continue
        if identity.key in contested:
            specific.append((index, identity, start, None))
            continue
        own = rows.by_google_id.get(ids[index])
        same = rows.newest(rows.by_specific[identity.specific_key])
        if own is not None and own in rows.by_key[identity.key]:
            holder = rows.holder(own)
        elif same is not None:
            holder = same
        else:
            holder = rows.newest(rows.by_key[identity.key])
        if holder is not None and distinct(identity, holder):
            specific.append((index, identity, start, holder.raw_id))
            continue
        ids[index] = holder.google_data_point_id if holder else new_record_id(identity.key)
        resolution.new_aliases.setdefault(identity.key, ids[index])

    if specific:
        await rows.find(
            (
                start
                for _, identity, start, _ in specific
                if start is not None and identity.key in alias
            ),
            (ids[index] for index, identity, _, _ in specific if identity.key in alias),
        )
        for index, identity, _, taken in specific:
            own = rows.by_google_id.get(ids[index])
            same = rows.newest(rows.by_specific[identity.specific_key])
            if own is not None and own in rows.by_specific[identity.specific_key]:
                holder = rows.holder(own)
            else:
                holder = same
            if holder is not None and holder.raw_id != taken:
                ids[index] = holder.google_data_point_id
            else:
                ids[index] = new_record_id(identity.specific_key)
            resolution.new_aliases.setdefault(identity.specific_key, ids[index])
    return resolution


async def add_aliases(session: AsyncSession, data_type: str, new_aliases: dict[str, str]) -> int:
    """Insert ``identity key -> row`` aliases (rows must exist); existing keys are kept."""
    if not new_aliases:
        return 0
    from app.services.storage import dialect_insert

    raw_ids = await rows_by_key(session, data_type, new_aliases.values())
    values = [
        {
            "data_type": data_type,
            "identity_key": key,
            "raw_id": raw_ids[google_id],
            "google_data_point_id": google_id,
        }
        for key, google_id in sorted(new_aliases.items())
        if google_id in raw_ids
    ]
    if not values:
        return 0
    statement = dialect_insert(session, RawHealthDataIdentity.__table__).on_conflict_do_nothing(
        index_elements=["data_type", "identity_key"]
    )
    await session.execute(statement, values)
    return len(values)


async def present_identities(
    session: AsyncSession,
    spec: DataTypeSpec,
    candidates: Sequence[tuple[uuid.UUID, object]],
    seen_since: datetime,
) -> set[uuid.UUID]:
    """Candidates (raw id, raw_json) whose record is still held by a row seen since then.

    A candidate's identity is present when the row its specific key resolves to, or else
    the row its v2 key resolves to, is another row and was seen at or after
    ``seen_since``. A row whose specific key resolves to itself is a source of its own:
    another source at the same instant does not keep it.
    """
    identities = {raw_id: record_identity(spec, raw_json) for raw_id, raw_json in candidates}
    keys = {
        key
        for identity in identities.values()
        if identity is not None
        for key in (identity.key, identity.specific_key)
    }
    alias = await aliases(session, spec.name, keys) if keys else {}
    current = await successors(session, {*alias.values(), *identities})
    rows = await stored_rows(session, set(current.values()))
    present: set[uuid.UUID] = set()
    for raw_id, identity in identities.items():
        holder = current[raw_id]
        if holder == raw_id and identity is not None:
            key = identity.specific_key if identity.specific_key in alias else identity.key
            if key in alias:
                holder = current[alias[key]]
        if holder != raw_id and rows[holder].last_seen_at >= as_utc(seen_since):
            present.add(raw_id)
    return present


_ALIAS_MISMATCH_SQL = text(
    "SELECT a.data_type, a.identity_key FROM raw_health_data_identity a "
    "JOIN raw_health_data r ON r.id = a.raw_id "
    "WHERE r.data_type <> a.data_type OR r.google_data_point_id <> a.google_data_point_id"
)
_LINK_MISMATCH_SQL = text(
    "SELECT s.old_raw_id FROM raw_health_data_supersessions s "
    "JOIN raw_health_data o ON o.id = s.old_raw_id JOIN raw_health_data n ON n.id = s.new_raw_id "
    "WHERE o.data_type <> s.data_type OR n.data_type <> s.data_type "
    "OR o.google_data_point_id <> s.old_google_data_point_id "
    "OR n.google_data_point_id <> s.new_google_data_point_id"
)
_LINK_CYCLE_SQL = text(
    "SELECT a.old_raw_id FROM raw_health_data_supersessions a "
    "JOIN raw_health_data_supersessions b ON b.old_raw_id = a.new_raw_id "
    "WHERE b.new_raw_id = a.old_raw_id"
)


async def verify_identities(session: AsyncSession) -> tuple[int, list[str]]:
    """Aliases and supersession links point at rows of their own data type and key."""
    errors: list[str] = []
    count = await session.scalar(select(func.count()).select_from(RawHealthDataIdentity)) or 0
    for data_type, key in (await session.execute(_ALIAS_MISMATCH_SQL)).tuples():
        errors.append(f"identity alias {data_type}/{key} does not match its raw row")
    for (raw_id,) in (await session.execute(_LINK_MISMATCH_SQL)).tuples():
        errors.append(f"supersession of raw row {raw_id} does not match its rows")
    for (raw_id,) in (await session.execute(_LINK_CYCLE_SQL)).tuples():
        errors.append(f"supersession of raw row {raw_id} forms a cycle")
    return count, errors
