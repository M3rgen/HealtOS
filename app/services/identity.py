"""Resolve records without a Google id to the raw row that holds them (v2 identities).

``resolve_ids`` decides, for one page of records, under which ``google_data_point_id`` each
is written. A record with a Google ``name`` keeps it. The others are grouped by v2 key
(app/parsers/identity.py) and, within a key, by source fingerprint; every group is paired
with at most one stored row of that key (the row that holds the record now, following
supersession links), and every stored row with at most one group:

1. a row of the *same* fingerprint (a returning record, civil times added, a grown interval);
2. else a row of a fingerprint in the same device chain (app/services/source_chains.py: a
   renamed device, a dropped package suffix) - the row gets the new payload as a new
   version, so nothing is archived twice;
3. else, when the stored rows of the key belong to other sources, the page may decide a new
   chain link (only under the rule of source_chains: the old source not seen in this fetch
   nor within five minutes before it, a unique candidate, complete sighting spans); then 2.
4. Otherwise the record is a source of its own: a new row, ``synthetic:v2:<hash>`` when
   nothing else was ever stored under the key, else ``synthetic:v2s:<hash>`` (key plus
   fingerprint).

There is no "newest row" fallback: a device's payload is never written onto another
device's row. Stored rows are found through the alias table (the key's alias, and the
fingerprint-specific alias, which leads straight to the row of the same source) and, where
that is not conclusive, among the rows with the same ``source_start_time`` or the record's v1
id (rows archived before v2 have no alias until identity-backfill gives them one). A raw
row's own key never changes: a returning record is written onto the row, so a changed
payload goes through the usual UPSERT and the version trigger keeps the old payload.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import any_, bindparam, func, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.logging import event
from app.core.timeutil import as_utc
from app.google_health.data_types import DataTypeSpec
from app.models.identity import RawHealthDataIdentity, RawHealthDataSupersession
from app.models.state import RawHealthData
from app.parsers.common import payload_hash, source_id
from app.parsers.identity import RecordIdentity, new_record_id, record_identity, source_label
from app.services.source_chains import (
    Chains,
    eligible_predecessors,
    insert_chain,
    load_chains,
    lock_data_type,
    record_sightings,
)

logger = logging.getLogger(__name__)

# Supersession links are followed at most this far (the backfill never links to a row that
# is superseded, nor supersedes a row that others point to, so one step is the norm).
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


@dataclass
class StoredRow:
    raw_id: uuid.UUID
    google_data_point_id: str
    payload_hash: str
    last_seen_at: datetime
    deleted: bool
    # Parsed from raw_json only when needed (``loaded``): an identical payload hash already
    # says "same record, same source".
    identity: RecordIdentity | None = None
    loaded: bool = False

    @property
    def order(self) -> tuple:
        """Most recent first when sorted in reverse: live, then last seen."""
        return (not self.deleted, self.last_seen_at, str(self.raw_id))


_LIGHT = (
    RawHealthData.id,
    RawHealthData.google_data_point_id,
    RawHealthData.payload_hash,
    RawHealthData.last_seen_at,
    RawHealthData.source_deleted_at,
)


class _Rows:
    """Stored rows met while resolving one page, and the rows that hold them now."""

    def __init__(self, session: AsyncSession, spec: DataTypeSpec) -> None:
        self.session, self.spec = session, spec
        self.info: dict[uuid.UUID, StoredRow] = {}
        self.current: dict[uuid.UUID, uuid.UUID] = {}
        self.by_key: dict[str, set[uuid.UUID]] = defaultdict(set)
        self.by_google_id: dict[str, uuid.UUID] = {}

    def _keep(self, row, raw_json=None, *, parse: bool = False) -> StoredRow:
        raw_id, google_id, digest, seen, deleted = row
        stored = self.info.get(raw_id)
        if stored is None:
            stored = StoredRow(raw_id, google_id, digest, as_utc(seen), deleted is not None)
            self.info[raw_id] = stored
            self.by_google_id[google_id] = raw_id
        if parse and not stored.loaded:
            stored.identity, stored.loaded = record_identity(self.spec, raw_json), True
            if stored.identity is not None:
                self.by_key[stored.identity.key].add(raw_id)
        return stored

    async def load(self, raw_ids: Iterable[uuid.UUID]) -> None:
        missing = sorted(set(raw_ids) - set(self.info))
        for chunk in _chunks(missing):
            rows = await self.session.execute(
                select(*_LIGHT).where(among(self.session, RawHealthData.id, chunk))
            )
            for row in rows.tuples():
                self._keep(row)

    async def identify(self, raw_ids: Iterable[uuid.UUID]) -> None:
        """Parse the identity of these (loaded) rows."""
        missing = sorted({raw_id for raw_id in raw_ids if not self.info[raw_id].loaded})
        for chunk in _chunks(missing):
            rows = await self.session.execute(
                select(*_LIGHT, RawHealthData.raw_json).where(
                    among(self.session, RawHealthData.id, chunk)
                )
            )
            for row in rows.tuples():
                self._keep(row[:5], row[5], parse=True)

    async def follow(self, raw_ids: Iterable[uuid.UUID]) -> None:
        wanted = set(raw_ids)
        todo = wanted - set(self.current)
        if todo:
            self.current.update(await successors(self.session, todo))
        await self.load(self.current[raw_id] for raw_id in wanted)

    def holder(self, raw_id: uuid.UUID) -> StoredRow:
        return self.info[self.current[raw_id]]

    async def find(self, starts: Iterable[datetime], google_ids: Iterable[str]) -> None:
        """Every stored row with one of these start times or keys (and its holder)."""
        conditions = [
            among(self.session, RawHealthData.source_start_time, chunk)
            for chunk in _chunks(sorted(set(starts)))
        ] + [
            among(self.session, RawHealthData.google_data_point_id, chunk)
            for chunk in _chunks(sorted(set(google_ids)))
        ]
        found: set[uuid.UUID] = set()
        for condition in conditions:
            rows = await self.session.execute(
                select(*_LIGHT, RawHealthData.raw_json).where(
                    RawHealthData.data_type == self.spec.name, condition
                )
            )
            for row in rows.tuples():
                self._keep(row[:5], row[5], parse=True)
                found.add(row[0])
        await self.follow(found)
        await self.identify(self.current[raw_id] for raw_id in found)

    def holders(self, key: str) -> list[StoredRow]:
        """The rows holding key ``key`` now (distinct, still of that key)."""
        seen: dict[uuid.UUID, StoredRow] = {}
        for raw_id in self.by_key.get(key, ()):
            holder = self.info[self.current.get(raw_id, raw_id)]
            if holder.identity is not None and holder.identity.key == key:
                seen[holder.raw_id] = holder
        return sorted(seen.values(), key=lambda row: str(row.raw_id))


@dataclass
class Resolution:
    # Per record, in page order: the google_data_point_id to write it under.
    ids: list[str]
    # identity key -> google_data_point_id, to alias once the rows exist.
    new_aliases: dict[str, str]
    # Source fingerprints that chains newly linked while resolving the page.
    chained: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class _Group:
    """The records of one page with one v2 key and one source fingerprint."""

    key: str
    source: str
    indexes: list[int]
    identity: RecordIdentity
    holder: StoredRow | None = None


def _prefer(rows: Sequence[StoredRow], group: _Group, ids: Sequence[str], alias_ids: set):
    """Among rows of one device: the aliased one, the record's own v1 row, the latest."""

    def rank(row: StoredRow) -> tuple:
        own = any(ids[index] == row.google_data_point_id for index in group.indexes)
        return (row.raw_id in alias_ids, own, *row.order)

    return max(rows, key=rank)


def _source(row: StoredRow) -> str:
    assert row.identity is not None  # holders() only returns rows with an identity
    return row.identity.source


def _pair(
    groups: list[_Group],
    holders: list[StoredRow],
    chains: Chains,
    ids: Sequence[str],
    alias_ids: set,
) -> None:
    """One-to-one: exact fingerprint first, then the closest fingerprint of the chain."""
    free = {row.raw_id: row for row in holders if row.identity is not None}
    taken = {group.holder.raw_id for group in groups if group.holder is not None}
    for row_id in taken:
        free.pop(row_id, None)
    for group in groups:
        if group.holder is not None:
            continue
        exact = [row for row in free.values() if _source(row) == group.source]
        if exact:
            group.holder = free.pop(_prefer(exact, group, ids, alias_ids).raw_id)
    for group in groups:
        if group.holder is not None:
            continue
        device = [row for row in free.values() if chains.same_device(_source(row), group.source)]
        if device:
            closest = max(chains.position(_source(row)) for row in device)
            nearest = [row for row in device if chains.position(_source(row)) == closest]
            group.holder = free.pop(_prefer(nearest, group, ids, alias_ids).raw_id)


async def resolve_ids(
    session: AsyncSession,
    spec: DataTypeSpec,
    records: Sequence[object],
    digests: Sequence[str],
    now: datetime,
    fetch_started: datetime | None = None,
) -> Resolution:
    """``digests``: each record's ``payload_hash``, in the same order. ``fetch_started``: when
    the fetch this page belongs to began (default ``now``: the page is a fetch of its own);
    a source last seen before it, by more than the chain gap, was not seen in this fetch."""
    started = as_utc(fetch_started or now)
    ids = [source_id(spec, record) for record in records]
    resolution = Resolution(ids, {})
    groups: dict[tuple[str, str], _Group] = {}
    seen: dict[str, tuple[str, str, object]] = {}
    for index, record in enumerate(records):
        identity = record_identity(spec, record)
        if identity is None:
            continue
        assert isinstance(record, dict)
        data_source = record.get("dataSource")
        seen.setdefault(identity.source, (identity.stable, source_label(data_source), data_source))
        group = groups.setdefault(
            (identity.key, identity.source), _Group(identity.key, identity.source, [], identity)
        )
        group.indexes.append(index)
    if not groups:
        return resolution

    await lock_data_type(session, spec.name, exclusive=False)
    # The page's sources are seen now: a source returned in this page is never "absent".
    await record_sightings(session, spec.name, seen, now)
    by_key: dict[str, list[_Group]] = defaultdict(list)
    for group in groups.values():
        by_key[group.key].append(group)
    for members in by_key.values():
        members.sort(key=lambda group: group.source)
    specific = {group.identity.specific_key: group for group in groups.values()}
    alias = await aliases(session, spec.name, [*by_key, *specific])
    # Rows archived before v2 have no alias: their own v1 id finds an unchanged record.
    unaliased = [
        group
        for group in groups.values()
        if group.key not in alias and group.identity.specific_key not in alias
    ]
    own = await rows_by_key(
        session, spec.name, {ids[index] for group in unaliased for index in group.indexes}
    )
    rows = _Rows(session, spec)
    await rows.follow([*alias.values(), *own.values()])
    alias_ids = {rows.current[raw_id] for raw_id in alias.values()}

    def candidates(group: _Group) -> list[StoredRow]:
        found = [
            rows.holder(alias[name])
            for name in (group.identity.specific_key, group.key)
            if name in alias
        ]
        found += [rows.holder(own[ids[index]]) for index in group.indexes if ids[index] in own]
        return found

    # Fast path 1: a row holding exactly this payload (same record, same source).
    claimed: dict[str, set[uuid.UUID]] = defaultdict(set)
    unchanged: set[str] = set()
    for group in groups.values():
        hashes = {digests[index] for index in group.indexes}
        for row in candidates(group):
            if row.payload_hash in hashes and row.raw_id not in claimed[group.key]:
                group.holder = row
                claimed[group.key].add(row.raw_id)
                break
    # A key whose records all came back unchanged under their own v1 id needs no alias yet
    # (the v1 id finds them again; identity-backfill aliases every key).
    for key, members in by_key.items():
        if key not in alias and all(
            group.holder is not None
            and group.identity.specific_key not in alias
            and group.holder.google_data_point_id == ids[group.indexes[0]]
            for group in members
        ):
            unchanged.add(key)
    # Fast path 2: a known row of this key and this source (a changed payload).
    pending = [group for group in groups.values() if group.holder is None]
    await rows.identify(row.raw_id for group in pending for row in candidates(group))
    unsettled: set[str] = set()
    for group in pending:
        for row in candidates(group):
            if (
                row.identity is not None
                and row.identity.key == group.key
                and row.identity.source == group.source
                and row.raw_id not in claimed[group.key]
            ):
                group.holder = row
                claimed[group.key].add(row.raw_id)
                break
        else:
            unsettled.add(group.key)

    if unsettled:
        # Every stored row of these keys: pair one-to-one by source and chain.
        await rows.find(
            (
                group.identity.start
                for key in unsettled
                for group in by_key[key]
                if group.identity.start is not None
            ),
            (ids[index] for key in unsettled for group in by_key[key] for index in group.indexes),
        )
        await rows.identify(
            holder.raw_id
            for key in unsettled
            for group in by_key[key]
            if (holder := group.holder) is not None
        )
        chains = await load_chains(
            session,
            spec.name,
            {*seen, *(row.identity.source for row in rows.info.values() if row.identity)},
        )
        for key in sorted(unsettled):
            _pair(by_key[key], rows.holders(key), chains, ids, alias_ids)
        before = dict(chains.following)
        await _decide_chains(session, spec, by_key, unsettled, rows, chains, started, now)
        if chains.following != before:
            for key in sorted(unsettled):
                _pair(by_key[key], rows.holders(key), chains, ids, alias_ids)
            resolution.chained = sorted(set(chains.following.items()) - set(before.items()))

    fresh: list[tuple[_Group, str]] = []
    for key, members in by_key.items():
        stored = key in alias or bool(rows.holders(key))
        lone = len(members) == 1
        for group in members:
            if group.holder is not None:
                target = group.holder.google_data_point_id
            else:
                plain = lone and not stored
                target = new_record_id(key if plain else group.identity.specific_key)
                fresh.append((group, target))
            for index in group.indexes:
                ids[index] = target
        if key not in unchanged:
            _plan_aliases(key, members, alias, rows, ids, resolution)
    await _unique_new_ids(session, spec, fresh, rows, ids, resolution)
    return resolution


def _plan_aliases(
    key: str,
    members: list[_Group],
    alias: dict[str, uuid.UUID],
    rows: _Rows,
    ids: list[str],
    resolution: Resolution,
) -> None:
    """The key's alias (when it has none), and a fingerprint-specific alias for every group
    the key's alias does not lead to (a second device, a renamed one): next time it is a
    direct hit."""
    targets = {group.source: ids[group.indexes[0]] for group in members}
    if key in alias:
        leads_to = rows.holder(alias[key]).google_data_point_id
    else:
        leads_to = targets[members[0].source]
        resolution.new_aliases.setdefault(key, leads_to)
    for group in members:
        target = targets[group.source]
        if target != leads_to and group.identity.specific_key not in alias:
            resolution.new_aliases.setdefault(group.identity.specific_key, target)


async def _decide_chains(
    session: AsyncSession,
    spec: DataTypeSpec,
    by_key: dict[str, list[_Group]],
    keys: Iterable[str],
    rows: _Rows,
    chains: Chains,
    started: datetime,
    now: datetime,
) -> None:
    """Rule 3 of the module docstring, per source: one decision for all of a page's keys."""
    devices: dict[str, set[str]] = defaultdict(set)
    shared: dict[tuple[str, str], int] = defaultdict(int)
    for key in keys:
        taken = {group.holder.raw_id for group in by_key[key] if group.holder is not None}
        others = [row for row in rows.holders(key) if row.raw_id not in taken and row.identity]
        for group in by_key[key]:
            if group.holder is not None:
                continue
            for row in others:
                devices[group.source].add(_source(row))
                shared[(group.source, _source(row))] += 1
    for new, found in sorted(devices.items()):
        span = chains.spans.get(new)
        if span is None:
            continue
        candidates = eligible_predecessors(chains, new, min(span.first, started), found)
        if len(candidates) == 1:
            old = candidates[0]
            members = set(chains.members(old))
            count = sum(
                n for (source, other), n in shared.items() if source == new and other in members
            )
            if await insert_chain(
                session, spec.name, chains, old, new, decided_by="live", shared_keys=count, now=now
            ):
                event(logger, "SOURCE_CHAIN_LINKED", data_type=spec.name, keys=count)
            else:
                # Another writer linked one of them first: use what is recorded now.
                recorded = await load_chains(session, spec.name, chains.spans)
                chains.following, chains.preceding = recorded.following, recorded.preceding
        elif candidates:
            event(logger, "SOURCE_CHAIN_AMBIGUOUS", data_type=spec.name, candidates=len(candidates))


async def _unique_new_ids(
    session: AsyncSession,
    spec: DataTypeSpec,
    fresh: list[tuple[_Group, str]],
    rows: _Rows,
    ids: list[str],
    resolution: Resolution,
) -> None:
    """A new row's id must not be one a stored row already has (that row may hold another
    source by now): such a record gets an id of its own."""
    if not fresh:
        return
    taken = set(await rows_by_key(session, spec.name, [target for _, target in fresh]))
    for group, target in fresh:
        if target not in taken:
            continue
        for attempt in range(1, 100):
            candidate = new_record_id(
                f"v2s:x:{payload_hash({'key': group.identity.specific_key, 'n': attempt})}"
            )
            if candidate not in taken and candidate not in rows.by_google_id:
                break
        taken.add(candidate)
        for index in group.indexes:
            ids[index] = candidate
        for key in (group.key, group.identity.specific_key):
            if resolution.new_aliases.get(key) == target:
                resolution.new_aliases[key] = candidate


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


async def present_records(
    session: AsyncSession,
    spec: DataTypeSpec,
    candidates: Sequence[tuple[uuid.UUID, object, datetime | None]],
    seen_since: datetime,
) -> set[uuid.UUID]:
    """Candidates (raw id, raw_json, stored start) whose record a row seen since then holds.

    Present means: another row with the same v2 key, of the *same device* (the same source
    fingerprint, or one linked to it by a valid chain), was seen at or after ``seen_since``
    - a duplicate of the record under an older id. A row of another source never keeps a
    candidate: two devices at one instant are two records, and one can be deleted alone.
    """
    identities = {raw_id: record_identity(spec, raw_json) for raw_id, raw_json, _ in candidates}
    starts = {identity.start for identity in identities.values() if identity and identity.start}
    # A stale stored start (written by an older parser) still finds the other rows.
    starts |= {as_utc(start) for _, _, start in candidates if start is not None}
    if not starts:
        return set()
    seen_rows: dict[str, set[str]] = defaultdict(set)
    for chunk in _chunks(sorted(starts)):
        rows = await session.execute(
            select(RawHealthData.raw_json).where(
                RawHealthData.data_type == spec.name,
                among(session, RawHealthData.source_start_time, chunk),
                RawHealthData.last_seen_at >= seen_since,
            )
        )
        for (raw_json,) in rows.tuples():
            identity = record_identity(spec, raw_json)
            if identity is not None:
                seen_rows[identity.key].add(identity.source)
    sources = {source for found in seen_rows.values() for source in found}
    sources |= {identity.source for identity in identities.values() if identity}
    chains = await load_chains(session, spec.name, sources)
    present: set[uuid.UUID] = set()
    for raw_id, identity in identities.items():
        if identity is None:
            continue
        if any(chains.same_device(identity.source, other) for other in seen_rows[identity.key]):
            present.add(raw_id)
    return present


# --- verify ------------------------------------------------------------------------------

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
# A link to a row that is itself superseded: every cycle, of any length, contains one.
_LINK_TARGET_SUPERSEDED_SQL = text(
    "SELECT a.old_raw_id FROM raw_health_data_supersessions a "
    "JOIN raw_health_data_supersessions b ON b.old_raw_id = a.new_raw_id"
)
_LINK_CYCLE_SQL = text(
    "WITH RECURSIVE walk(start_id, current_id, depth) AS ("
    " SELECT old_raw_id, new_raw_id, 1 FROM raw_health_data_supersessions"
    " UNION ALL"
    " SELECT w.start_id, s.new_raw_id, w.depth + 1 FROM walk w"
    " JOIN raw_health_data_supersessions s ON s.old_raw_id = w.current_id"
    " WHERE w.depth < 64 AND w.current_id <> w.start_id)"
    " SELECT DISTINCT start_id FROM walk WHERE current_id = start_id"
)
# A record Google named itself is never a duplicate of another row.
_LINK_NAMED_SQL = text(
    "SELECT old_raw_id FROM raw_health_data_supersessions "
    "WHERE old_google_data_point_id NOT LIKE 'synthetic:%'"
)
# Linked to a row that was already marked deleted while the linked row was not: the link
# hid a live record behind a deleted one.
_LINK_TO_DELETED_SQL = text(
    "SELECT s.old_raw_id FROM raw_health_data_supersessions s "
    "JOIN raw_health_data o ON o.id = s.old_raw_id JOIN raw_health_data n ON n.id = s.new_raw_id "
    "WHERE n.source_deleted_at IS NOT NULL AND n.source_deleted_at <= s.created_at "
    "AND o.source_deleted_at IS NULL"
)
_CHAIN_CYCLE_SQL = text(
    "WITH RECURSIVE walk(data_type, start_fp, current_fp, depth) AS ("
    " SELECT data_type, from_fingerprint, to_fingerprint, 1 FROM raw_source_chains"
    " UNION ALL"
    " SELECT w.data_type, w.start_fp, c.to_fingerprint, w.depth + 1 FROM walk w"
    " JOIN raw_source_chains c ON c.data_type = w.data_type AND c.from_fingerprint = w.current_fp"
    " WHERE w.depth < 64 AND w.current_fp <> w.start_fp)"
    " SELECT DISTINCT data_type, start_fp FROM walk WHERE current_fp = start_fp"
)


async def verify_identities(session: AsyncSession) -> tuple[int, list[str], list[str]]:
    """Aliases, supersession links and source chains are consistent with their rows.

    Returns (aliases, errors, notes). Besides the key checks: no link forms a cycle or
    points at a superseded row, no link hides a record with a Google id or a live record
    behind a deleted one, every link joins two rows of one v2 key and one device (the same
    fingerprint or a recorded chain), and no chain forms a cycle. A chain its sightings
    contradict today (both forms seen together since) is a note: it is no longer used, its
    sources are kept apart, and the rows it paired kept every payload as a version.
    """
    notes: list[str] = []
    from app.services.raw_history import PROTECTIVE_TRIGGERS

    guarded = {table for table, _, _ in PROTECTIVE_TRIGGERS}
    from app.services.identity_backfill import spec_for

    errors: list[str] = await verify_identity_schema(session, guarded)
    count = await session.scalar(select(func.count()).select_from(RawHealthDataIdentity)) or 0
    for data_type, key in (await session.execute(_ALIAS_MISMATCH_SQL)).tuples():
        errors.append(f"identity alias {data_type}/{key} does not match its raw row")
    for (raw_id,) in (await session.execute(_LINK_MISMATCH_SQL)).tuples():
        errors.append(f"supersession of raw row {raw_id} does not match its rows")
    for (raw_id,) in (await session.execute(_LINK_TARGET_SUPERSEDED_SQL)).tuples():
        errors.append(f"supersession of raw row {raw_id} points at a superseded row")
    for (raw_id,) in (await session.execute(_LINK_CYCLE_SQL)).tuples():
        errors.append(f"supersession of raw row {raw_id} forms a cycle")
    for (raw_id,) in (await session.execute(_LINK_NAMED_SQL)).tuples():
        errors.append(f"supersession of raw row {raw_id} hides a record with a Google id")
    for (raw_id,) in (await session.execute(_LINK_TO_DELETED_SQL)).tuples():
        errors.append(f"supersession of raw row {raw_id} hides a live record behind a deleted one")
    for data_type, fingerprint in (await session.execute(_CHAIN_CYCLE_SQL)).tuples():
        errors.append(f"source chain of {data_type} from {fingerprint[:12]} forms a cycle")
    chains: dict[str, Chains] = {}
    old_row, new_row = aliased(RawHealthData), aliased(RawHealthData)
    links = (
        select(
            RawHealthDataSupersession.old_raw_id,
            RawHealthDataSupersession.data_type,
            old_row.raw_json,
            new_row.raw_json,
        )
        .join(old_row, old_row.id == RawHealthDataSupersession.old_raw_id)
        .join(new_row, new_row.id == RawHealthDataSupersession.new_raw_id)
    )
    result = await session.stream(links.execution_options(yield_per=1_000))
    async for raw_id, data_type, old_json, new_json in result:
        spec = spec_for(data_type)
        old, new = record_identity(spec, old_json), record_identity(spec, new_json)
        if old is None or new is None or old.key != new.key:
            errors.append(f"supersession of raw row {raw_id} links two different records")
            continue
        if data_type not in chains:
            chains[data_type] = await load_chains(session, data_type)
        if not chains[data_type].same_device(old.source, new.source):
            errors.append(f"supersession of raw row {raw_id} links two different sources")
    for data_type in sorted(set(chains) | set(await _chained_types(session))):
        current = chains.get(data_type) or await load_chains(session, data_type)
        for old, new in sorted(current.following.items()):
            if not current.valid(old, new):
                notes.append(
                    f"source chain of {data_type} {current.spans[old].label!r} -> "
                    f"{current.spans[new].label!r} is contradicted by its sightings "
                    "(both seen together since): its sources are kept apart"
                )
    return count, errors, notes


# The constraints of migration 20261021_0012, as pg_get_constraintdef prints them.
EXPECTED_CONSTRAINTS = {
    "raw_health_data_identity": {
        "pk_raw_health_data_identity": "PRIMARY KEY (data_type, identity_key)",
        "fk_raw_health_data_identity_raw_id_raw_health_data": (
            "FOREIGN KEY (raw_id) REFERENCES raw_health_data(id)"
        ),
    },
    "raw_health_data_supersessions": {
        "pk_raw_health_data_supersessions": "PRIMARY KEY (old_raw_id)",
        "ck_raw_health_data_supersessions_distinct_rows": "CHECK ((old_raw_id <> new_raw_id))",
        "fk_raw_health_data_supersessions_old_raw_id_raw_health_data": (
            "FOREIGN KEY (old_raw_id) REFERENCES raw_health_data(id)"
        ),
        "fk_raw_health_data_supersessions_new_raw_id_raw_health_data": (
            "FOREIGN KEY (new_raw_id) REFERENCES raw_health_data(id)"
        ),
    },
    "raw_source_chains": {
        "pk_raw_source_chains": "PRIMARY KEY (data_type, from_fingerprint)",
        "uq_raw_source_chains_data_type": "UNIQUE (data_type, to_fingerprint)",
        "ck_raw_source_chains_distinct_sources": (
            "CHECK (((from_fingerprint)::text <> (to_fingerprint)::text))"
        ),
    },
}
_CONSTRAINTS_SQL = text(
    "SELECT c.relname, k.conname, pg_get_constraintdef(k.oid), k.convalidated, k.condeferrable "
    "FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = current_schema() AND c.relname = ANY(:tables)"
)
_RULES_SQL = text(
    "SELECT tablename, rulename FROM pg_rules "
    "WHERE schemaname = current_schema() AND tablename = ANY(:tables)"
)


async def verify_identity_schema(session: AsyncSession, guarded: Iterable[str]) -> list[str]:
    """PostgreSQL: the identity tables keep their keys and checks (a dropped primary key
    would let a row be superseded twice, a dropped check let a row supersede itself), and
    no rule sits on a guarded table (``DO INSTEAD`` would bypass the triggers)."""
    if session.get_bind().dialect.name != "postgresql":
        return []
    errors: list[str] = []
    found: dict[str, dict[str, tuple]] = defaultdict(dict)
    rows = await session.execute(_CONSTRAINTS_SQL, {"tables": sorted(EXPECTED_CONSTRAINTS)})
    for table, name, definition, validated, deferrable in rows.tuples():
        found[table][name] = (definition, validated, deferrable)
    for table, expected in EXPECTED_CONSTRAINTS.items():
        for name, definition in expected.items():
            actual = found[table].pop(name, None)
            if actual is None:
                errors.append(f"constraint missing: {table}.{name}")
            elif actual[0] != definition or not actual[1] or actual[2]:
                errors.append(f"constraint {table}.{name} was changed: {actual[0]}")
        for name, (definition, _, _) in sorted(found[table].items()):
            errors.append(f"unexpected constraint on {table}: {name} {definition}")
    for table, rule in (await session.execute(_RULES_SQL, {"tables": sorted(guarded)})).tuples():
        errors.append(f"rule {rule} on guarded table {table} (it could bypass the triggers)")
    return errors


async def _chained_types(session: AsyncSession) -> list[str]:
    from app.models.identity import RawSourceChain

    return list((await session.scalars(select(RawSourceChain.data_type).distinct())).all())
