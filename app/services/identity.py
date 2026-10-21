"""Resolve records without a Google id to the raw row that holds them (v2 identities).

``resolve_ids`` decides, for one page of records, under which ``google_data_point_id`` each
is written. A record with a Google ``name`` keeps it. The others are grouped by v2 key
(app/parsers/identity.py) and source fingerprint. The write path never *decides* whether two
fingerprints are one device; it only applies what the reconciler or the owner decided
(app/services/source_chains.py: a *device* is a fingerprint and every one joined to it by
usable chain links, either way). Every group is written onto

1. the row that holds this key from this *same* fingerprint and is not superseded (a
   returning record, civil times added, a grown interval, a revised value); or
2. when the fingerprint's own rows are all superseded (merged by the reconciler into a row
   of the device): the row that holds them, when it is the same device and holds the same
   reading (it gets this form as a new version; a deletion mark on it is cleared); else
   the own row itself when that row would count (its holder is marked deleted, or this page
   writes the holder with the same reading); else a new row (a different value is never
   hidden behind another row); or
3. else the row of another fingerprint of the device that holds this key and the same
   reading (a rename, a dropped package suffix, the old name come back): a new version; or
4. else a new row ``synthetic:v2s:<hash of key and fingerprint>``: an unknown fingerprint
   always gets rows of its own. A renamed device is then counted twice until the reconciler
   links the rows (nightly); a genuinely distinct device is never hidden. When that id is
   taken (its row holds another value of the record now, merged by the reconciler), the row
   gets the first free *spare* id of the record and fingerprint.

Stored rows are found by id only (no scan): the record's v1 id (rows archived before v2), the
v2s ids of the key and each fingerprint of the device (rows stored since), its spare ids when
its v2s id is taken (in blocks, until one is free: spare ids are given in order, so the row
written last time is found again and never a second one made), and the alias table (rows whose
own id is neither). Superseded means superseded by a link *in force* (its two fingerprints one
device now): a row whose links are no longer in force is its own holder again. A payload is
never written onto a row of another device, nor onto a row of the device that measured
something else. ``store_records`` locks the rows it writes onto and re-resolves when one
changed meanwhile, and creates new rows with ``ON CONFLICT DO NOTHING`` (re-resolving when
another writer created the row first).

``present_records`` decides, for deletion marking, whether an unseen row's record is still
returned: with the same reading under a row it is linked with or a row of its device
(present); not by its device, which returned the key with another value (missing: the row
holds a value no longer returned); with the same reading under a fingerprint not established
distinct from its device, or distinct by evidence alone while it looks like a rename
(deferred: possibly a rename nobody decided yet); or not at all (missing).
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import (
    String,
    any_,
    bindparam,
    case,
    func,
    literal,
    null,
    select,
    text,
    union_all,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.google_health.data_types import DataTypeSpec
from app.models.identity import (
    RawHealthDataIdentity,
    RawHealthDataSupersession,
    in_force_sql,
)
from app.models.state import RawHealthData
from app.parsers.common import google_name, source_id
from app.parsers.identity import (
    RecordIdentity,
    new_record_id,
    reading,
    record_identity,
    source_label,
    spare_record_id,
    specific_key,
)
from app.services.source_chains import (
    Chains,
    SourceState,
    load_chains,
    load_state,
    lock_data_type,
)

logger = logging.getLogger(__name__)

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


async def _find(
    session: AsyncSession, data_type: str, keys: set[str], google_ids: set[str]
) -> tuple[dict[str, uuid.UUID], dict[str, uuid.UUID]]:
    """(alias key -> raw id, google id -> raw id) in one statement per chunk."""
    alias: dict[str, uuid.UUID] = {}
    by_id: dict[str, uuid.UUID] = {}
    keys_left, ids_left = sorted(keys), sorted(google_ids)
    table = RawHealthDataIdentity
    while keys_left or ids_left:
        key_chunk, keys_left = keys_left[:_CHUNK], keys_left[_CHUNK:]
        id_chunk, ids_left = ids_left[:_CHUNK], ids_left[_CHUNK:]
        parts = []
        if key_chunk:
            parts.append(
                select(literal("a", String), table.identity_key, table.raw_id).where(
                    table.data_type == data_type, among(session, table.identity_key, key_chunk)
                )
            )
        if id_chunk:
            parts.append(
                select(
                    literal("r", String), RawHealthData.google_data_point_id, RawHealthData.id
                ).where(
                    RawHealthData.data_type == data_type,
                    among(session, RawHealthData.google_data_point_id, id_chunk),
                )
            )
        statement = union_all(*parts) if len(parts) > 1 else parts[0]
        for kind, name, raw_id in (await session.execute(statement)).tuples():
            (alias if kind == "a" else by_id)[name] = raw_id
    return alias, by_id


async def successors(session: AsyncSession, raw_ids: Iterable[uuid.UUID], chains: Chains) -> dict:
    """raw id -> the row that holds its record now: the target of its newest link in force
    (``chains``: its two fingerprints are one device) - a live target before one marked
    deleted, as readers hide a row behind any live holder - followed; itself when it has
    none."""
    result = {raw_id: raw_id for raw_id in raw_ids}
    link = RawHealthDataSupersession
    step: dict[uuid.UUID, uuid.UUID | None] = {}
    visited = {raw_id: {raw_id} for raw_id in result}
    frontier = sorted(set(result.values()))
    for _ in range(MAX_CHAIN):
        for chunk in _chunks([raw_id for raw_id in frontier if raw_id not in step]):
            newest: dict[uuid.UUID, tuple] = {}
            rows = await session.execute(
                select(
                    link.old_raw_id,
                    link.new_raw_id,
                    link.old_fingerprint,
                    link.new_fingerprint,
                    link.created_at,
                    RawHealthData.source_deleted_at.is_(None),
                )
                .join(RawHealthData, RawHealthData.id == link.new_raw_id)
                .where(among(session, link.old_raw_id, chunk))
            )
            for old, new, old_source, new_source, created, live in rows.tuples():
                if not chains.in_force(old_source, new_source):
                    continue
                rank = (bool(live), created, str(new))
                if old not in newest or rank > newest[old][0]:
                    newest[old] = (rank, new)
            for raw_id in chunk:
                step[raw_id] = newest[raw_id][1] if raw_id in newest else None
        further: set[uuid.UUID] = set()
        for start, current in result.items():
            target = step.get(current)
            if target is not None and target not in visited[start]:
                visited[start].add(target)
                result[start] = target
                further.add(target)
        if not further:
            break
        frontier = sorted(further)
    return result


@dataclass
class StoredRow:
    raw_id: uuid.UUID
    google_data_point_id: str
    payload_hash: str
    last_seen_at: datetime
    deleted: bool
    identity: RecordIdentity | None = None
    reading: str | None = None
    # The row that holds this row's record now (itself unless superseded).
    holder: uuid.UUID | None = None

    @property
    def order(self) -> tuple:
        """Most recent first when sorted in reverse: live, then last seen."""
        return (not self.deleted, self.last_seen_at, str(self.raw_id))


@dataclass
class Resolution:
    # Per record, in page order: the google_data_point_id to write it under.
    ids: list[str]
    # specific identity key -> google_data_point_id, to alias once the rows exist.
    new_aliases: dict[str, str] = field(default_factory=dict)
    # Existing rows the page writes onto: raw id -> the payload hash resolved against.
    expected: dict[uuid.UUID, str] = field(default_factory=dict)
    # google_data_point_ids of the rows this page creates.
    created: set[str] = field(default_factory=set)
    # Records written onto a row of another fingerprint of their device.
    chained: int = 0
    # Records kept on a row of their own because the device's row measured something else.
    kept_apart: int = 0
    # The page's sources: fingerprint -> (stable class, label, dataSource); recorded as
    # sightings late in the transaction (storage), with the response time.
    seen: dict[str, tuple[str, str, object]] = field(default_factory=dict)
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
    # The reading of its last record (what its row holds after the page).
    reading: str | None = None


_LIGHT = (
    RawHealthData.id,
    RawHealthData.google_data_point_id,
    RawHealthData.payload_hash,
    RawHealthData.last_seen_at,
    RawHealthData.source_deleted_at,
)


async def _load_rows(
    session: AsyncSession, spec: DataTypeSpec, raw_ids: set[uuid.UUID], known: dict[str, tuple]
) -> dict[uuid.UUID, StoredRow]:
    """The rows, with their identity and reading. ``known``: payload hash -> (identity,
    reading) of the page's records; a row holding exactly such a payload is not read whole."""
    from app.core.timeutil import as_utc

    found: dict[uuid.UUID, StoredRow] = {}
    digests = sorted(known)
    for chunk in _chunks(sorted(raw_ids)):
        payload = RawHealthData.raw_json
        if digests:
            payload = case(
                (among(session, RawHealthData.payload_hash, digests), null()),
                else_=RawHealthData.raw_json,
            )
        rows = await session.execute(
            select(*_LIGHT, payload).where(among(session, RawHealthData.id, chunk))
        )
        for raw_id, google_id, digest, seen, deleted, raw_json in rows.tuples():
            row = StoredRow(raw_id, google_id, digest, as_utc(seen), deleted is not None)
            if digest in known:
                row.identity, row.reading = known[digest]
            else:
                row.identity = record_identity(spec, raw_json)
                row.reading = reading(spec, raw_json)
            found[raw_id] = row
    return found


async def resolve_ids(
    session: AsyncSession,
    spec: DataTypeSpec,
    records: Sequence[object],
    digests: Sequence[str],
) -> Resolution:
    """``digests``: each record's ``payload_hash``, in the same order."""
    ids = [source_id(spec, record) for record in records]
    resolution = Resolution(ids)
    groups: dict[tuple[str, str], _Group] = {}
    known: dict[str, tuple] = {}
    for index, record in enumerate(records):
        identity = record_identity(spec, record)
        if identity is None:
            continue
        assert isinstance(record, dict)
        data_source = record.get("dataSource")
        resolution.seen.setdefault(
            identity.source, (identity.stable, source_label(data_source), data_source)
        )
        group = groups.setdefault(
            (identity.key, identity.source), _Group(identity.key, identity.source, identity)
        )
        group.indexes.append(index)
        group.reading = reading(spec, record)
        known[digests[index]] = (identity, group.reading)
    if not groups:
        return resolution

    await lock_data_type(session, spec.name, exclusive=False)
    state = await load_state(session, spec.name, resolution.seen)
    resolution.state = state
    chains = state.chains
    by_key: dict[str, list[_Group]] = defaultdict(list)
    for group in groups.values():
        by_key[group.key].append(group)
    # One response returned this key under two fingerprints with two values: evidence of
    # two devices (one record has one value).
    resolution.evidence = [
        (left.source, right.source, key)
        for key, found in by_key.items()
        for position, left in enumerate(found)
        for right in found[position + 1 :]
        if left.reading != right.reading
    ]

    # First by id only (the record's v1 id and its own v2s id): a returning record of a
    # known fingerprint is found there, as a rule. Then, for the groups without a current
    # row of their own, by alias and by the ids of the other fingerprints of the device.
    rows: dict[uuid.UUID, StoredRow] = {}
    alias: dict[str, uuid.UUID] = {}
    by_id: dict[str, uuid.UUID] = {}

    async def look_up(keys: set[str], google_ids: set[str]) -> None:
        found_alias, found_ids = await _find(session, spec.name, keys, google_ids)
        alias.update(found_alias)
        by_id.update(found_ids)
        found = (set(found_alias.values()) | set(found_ids.values())) - set(rows)
        if not found:
            return
        holders = await successors(session, found, chains)
        loaded = await _load_rows(session, spec, (found | set(holders.values())) - set(rows), known)
        for row in loaded.values():
            row.holder = holders.get(row.raw_id, row.raw_id)
        rows.update(loaded)

    await look_up(
        set(),
        {
            google_id
            for group in groups.values()
            for google_id in (
                new_record_id(group.identity.specific_key),
                *(ids[index] for index in group.indexes),
            )
        },
    )
    # A record whose own v2s id is taken by a row that is not its current row (superseded, or
    # holding another form now) was given spare ids: they are found the same way, by id, in
    # blocks (spare ids are taken in order, so the first block with a free one is the last).
    pending = [
        group
        for name, group in groups.items()
        if (taken := by_id.get(new_record_id(group.identity.specific_key))) is not None
        and not _current(rows.get(taken), name)
    ]
    attempt = 1
    while pending:
        block = range(attempt, attempt + SPARE_BLOCK)
        await look_up(
            set(),
            {spare_record_id(group.identity, number) for group in pending for number in block}
            - set(by_id),
        )
        pending = [
            group
            for group in pending
            if all(spare_record_id(group.identity, number) in by_id for number in block)
        ]
        attempt += SPARE_BLOCK
    settled = {
        (row.identity.key, row.identity.source)
        for row in rows.values()
        if row.identity is not None and row.holder == row.raw_id
    }
    wanted_keys: set[str] = set()
    wanted_ids: set[str] = set()
    for name, group in groups.items():
        if name in settled:
            continue
        for source in chains.members(group.source):
            key = specific_key(group.key, source)
            wanted_keys.add(key)
            wanted_ids.add(new_record_id(key))
    if wanted_keys:
        await look_up(wanted_keys, wanted_ids - set(by_id))

    by_identity: dict[tuple[str, str], list[StoredRow]] = defaultdict(list)
    rows_of_key: dict[str, list[StoredRow]] = defaultdict(list)
    for row in rows.values():
        if row.identity is not None:
            by_identity[(row.identity.key, row.identity.source)].append(row)
            rows_of_key[row.identity.key].append(row)

    targets: dict[tuple[str, str], StoredRow] = {}
    # raw id -> the group writing onto it.
    claimed: dict[uuid.UUID, _Group] = {}

    def claim(name: tuple[str, str], target: StoredRow) -> None:
        targets[name] = target
        claimed[target.raw_id] = groups[name]

    # 1. The fingerprint's own row that holds the record.
    for name in sorted(groups):
        current = [row for row in by_identity.get(name, []) if row.holder == row.raw_id]
        if current:
            claim(name, max(current, key=lambda row: row.order))
    fresh_names: set[tuple[str, str]] = set()
    # 2. Its own rows are all superseded: the row holding them, or the own row if it counts.
    for name, group in sorted(groups.items()):
        own = by_identity.get(name, [])
        if name in targets or not own:
            continue
        mine = max(own, key=lambda row: row.order)
        holder = rows.get(mine.holder) if mine.holder is not None else None
        same = (
            holder is not None
            and holder.identity is not None
            and holder.identity.key == group.key
            and chains.same_device(group.source, holder.identity.source)
        )
        writer = claimed.get(holder.raw_id) if holder is not None else None
        if writer is not None:
            if same and writer.reading == group.reading:
                # Hidden behind its holder, which this page writes with the same reading.
                targets[name] = mine
            else:
                fresh_names.add(name)
        elif holder is not None and same and holder.reading == group.reading:
            claim(name, holder)
            resolution.chained += len(group.indexes)
        elif holder is not None and holder.deleted:
            # The holder is marked deleted: the own row counts again (held_elsewhere).
            targets[name] = mine
        else:
            fresh_names.add(name)
            resolution.kept_apart += len(group.indexes)
    # 3. Another fingerprint of the device holds this key with the same reading.
    for name, group in sorted(groups.items()):
        if name in targets or name in fresh_names:
            continue
        matches = [
            row
            for row in rows_of_key.get(group.key, [])
            if row.holder == row.raw_id
            and row.raw_id not in claimed
            and row.identity is not None
            and row.identity.source != group.source
            and chains.same_device(group.source, row.identity.source)
            and row.reading == group.reading
        ]
        if matches:
            claim(name, max(matches, key=lambda row: row.order))
            resolution.chained += len(group.indexes)
    fresh: list[tuple[_Group, str]] = []
    for name, group in sorted(groups.items()):
        own_key = group.identity.specific_key
        target = targets.get(name)
        if target is None:
            fresh.append((group, new_record_id(own_key)))
            continue
        resolution.expected[target.raw_id] = target.payload_hash
        # Found by the record's own ids (its v1 id, its v2s id) again next time: no alias.
        own_ids = {new_record_id(own_key), *(ids[index] for index in group.indexes)}
        for index in group.indexes:
            ids[index] = target.google_data_point_id
        if target.google_data_point_id not in own_ids and own_key not in alias:
            resolution.new_aliases[own_key] = target.google_data_point_id
    if fresh:
        _new_ids(fresh, by_id, ids, resolution)
    return resolution


def _current(row: StoredRow | None, name: tuple[str, str]) -> bool:
    """``row`` holds the record ``name`` (key, fingerprint) and is not superseded."""
    return (
        row is not None
        and row.identity is not None
        and (row.identity.key, row.identity.source) == name
        and row.holder == row.raw_id
    )


def _new_ids(
    fresh: list[tuple[_Group, str]],
    by_id: dict[str, uuid.UUID],
    ids: list[str],
    resolution: Resolution,
) -> None:
    """A new row's id must not be one a stored row already has (that row holds another
    form or value of the record now): such a record gets the first free spare id (the
    stored spare ids of the record were looked up by ``resolve_ids``)."""
    for group, target in fresh:
        if target in by_id:
            number = 1
            while (target := spare_record_id(group.identity, number)) in by_id or (
                target in resolution.created
            ):
                number += 1
            resolution.new_aliases.setdefault(group.identity.specific_key, target)
        resolution.created.add(target)
        for index in group.indexes:
            ids[index] = target


async def add_aliases(
    session: AsyncSession,
    data_type: str,
    new_aliases: dict[str, str],
    raw_ids: dict[str, uuid.UUID] | None = None,
) -> int:
    """Insert ``identity key -> row`` aliases (rows must exist); existing keys are kept.
    ``raw_ids``: google id -> raw id of the rows, when the caller knows them."""
    if not new_aliases:
        return 0
    from app.services.storage import dialect_insert

    if raw_ids is None or any(google_id not in raw_ids for google_id in new_aliases.values()):
        raw_ids = {**(raw_ids or {}), **await rows_by_key(session, data_type, new_aliases.values())}
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
    for offset in range(0, len(values), 1000):
        await session.execute(statement, values[offset : offset + 1000])
    return len(values)


@dataclass
class Presence:
    # Candidates whose record (same reading) a row they are linked with, or a row of their
    # device, was seen holding.
    present: set[uuid.UUID] = field(default_factory=set)
    # Candidates whose record (same reading) a fingerprint that may be their device was seen
    # holding (a possible rename nobody decided): not marked now.
    deferred: set[uuid.UUID] = field(default_factory=set)


async def present_records(
    session: AsyncSession,
    spec: DataTypeSpec,
    candidates: Sequence[tuple[uuid.UUID, object, datetime | None]],
    seen_since: datetime,
    chains: Chains | None = None,
) -> Presence:
    """Candidates (raw id, raw_json, stored start) that must not be marked deleted.

    For each candidate, the rows with its v2 key seen at or after ``seen_since`` decide:

    * one *with the same reading* it is linked with (the same holder through links in force),
      or of its *device* (the same fingerprint, or one joined to it by live chain links) - a
      duplicate of the record under another id: present;
    * else, when its device returned the key with *another* reading: missing - the row holds
      a value its device no longer returns (one device returns one value per record, so a
      same reading under another fingerprint is another device's);
    * else one with the same reading of a fingerprint not established distinct from the
      candidate's device, or distinct by evidence alone while their sightings only touch (it
      looks like a rename): deferred, until the reconciler or the owner has decided;
    * else missing. Of two devices that recorded at one instant, one can be deleted alone.

    Nothing is written here (evidence of two devices comes only from responses, in the
    write path). ``chains``: the data type's chains with every span (loaded when not given).
    """
    from app.core.timeutil import as_utc

    presence = Presence()
    identities = {raw_id: record_identity(spec, raw_json) for raw_id, raw_json, _ in candidates}
    values = {raw_id: reading(spec, raw_json) for raw_id, raw_json, _ in candidates}
    starts = {identity.start for identity in identities.values() if identity and identity.start}
    # A stale stored start (written by an older parser) still finds the other rows.
    starts |= {as_utc(start) for _, _, start in candidates if start is not None}
    if not starts:
        return presence
    seen_rows: dict[str, list[tuple[uuid.UUID, str, str | None]]] = defaultdict(list)
    for chunk in _chunks(sorted(starts)):
        rows = await session.execute(
            select(RawHealthData.id, RawHealthData.raw_json).where(
                RawHealthData.data_type == spec.name,
                among(session, RawHealthData.source_start_time, chunk),
                RawHealthData.last_seen_at >= seen_since,
            )
        )
        for raw_id, raw_json in rows.tuples():
            identity = record_identity(spec, raw_json)
            if identity is not None:
                seen_rows[identity.key].append((raw_id, identity.source, reading(spec, raw_json)))
    if not seen_rows:
        return presence
    if chains is None:
        chains = await load_chains(session, spec.name)
    holders = await successors(
        session,
        [*identities, *(raw_id for found in seen_rows.values() for raw_id, _, _ in found)],
        chains,
    )
    for raw_id, identity in identities.items():
        if identity is None:
            continue
        others = [found for found in seen_rows.get(identity.key, []) if found[0] != raw_id]
        same_value = [
            (other_id, source) for other_id, source, value in others if value == values[raw_id]
        ]
        if any(
            holders.get(other_id) == holders.get(raw_id)
            or chains.same_device(identity.source, source)
            for other_id, source in same_value
        ):
            presence.present.add(raw_id)
        elif any(
            chains.same_device(identity.source, source) and value != values[raw_id]
            for _, source, value in others
        ):
            continue
        elif any(
            not chains.distinct(identity.source, source)
            or chains.looks_like_rename(identity.source, source)
            for _, source in same_value
        ):
            presence.deferred.add(raw_id)
    return presence


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
# Every cycle of links, of any state (a cycle in force would hide all its rows).
_LINK_CYCLE_SQL = text(
    "WITH RECURSIVE walk(start_id, current_id, depth) AS ("
    " SELECT old_raw_id, new_raw_id, 1 FROM raw_health_data_supersessions"
    " UNION"
    " SELECT w.start_id, s.new_raw_id, w.depth + 1 FROM walk w"
    " JOIN raw_health_data_supersessions s ON s.old_raw_id = w.current_id"
    " WHERE w.depth < 64 AND w.current_id <> w.start_id)"
    " SELECT DISTINCT start_id FROM walk WHERE current_id = start_id"
)
# Live rows superseded only by links in force whose holders are all marked deleted: readers
# count them again.
_COUNTED_AGAIN_SQL = text(
    "SELECT count(DISTINCT s.old_raw_id) FROM raw_health_data_supersessions s "
    "JOIN raw_health_data o ON o.id = s.old_raw_id "
    f"WHERE o.source_deleted_at IS NULL AND {in_force_sql('s')} "
    "AND NOT EXISTS (SELECT 1 FROM raw_health_data_supersessions s2 "
    "JOIN raw_health_data h ON h.id = s2.new_raw_id WHERE s2.old_raw_id = s.old_raw_id "
    f"AND h.source_deleted_at IS NULL AND {in_force_sql('s2')})"
)
# Every identity-merged event (raw_health_data_events, guarded by 0011) has its link: a
# supersession table that was emptied or swapped for an empty copy shows up here.
_MERGED_WITHOUT_LINK_SQL = text(
    "SELECT e.raw_id FROM raw_health_data_events e WHERE e.event = 'identity-merged' "
    "AND NOT EXISTS (SELECT 1 FROM raw_health_data_supersessions s WHERE s.old_raw_id = e.raw_id)"
)


@dataclass
class _ChainRow:
    old: str
    new: str
    decided_by: str
    evidence: str
    created_at: datetime


async def _chain_rows(session: AsyncSession) -> dict[str, list[_ChainRow]]:
    from app.core.timeutil import as_utc
    from app.models.identity import RawSourceChain

    found: dict[str, list[_ChainRow]] = defaultdict(list)
    rows = await session.execute(
        select(
            RawSourceChain.data_type,
            RawSourceChain.from_fingerprint,
            RawSourceChain.to_fingerprint,
            RawSourceChain.decided_by,
            RawSourceChain.evidence,
            RawSourceChain.created_at,
        ).order_by(RawSourceChain.id)
    )
    for data_type, old, new, decided_by, evidence, created in rows.tuples():
        found[data_type].append(_ChainRow(old, new, decided_by, evidence, as_utc(created)))
    return found


async def _decision_rows(session: AsyncSession) -> dict[int, tuple]:
    from app.models.identity import RawSourceDecision

    rows = await session.execute(
        select(
            RawSourceDecision.id,
            RawSourceDecision.data_type,
            RawSourceDecision.decision,
            RawSourceDecision.first_fingerprint,
            RawSourceDecision.second_fingerprint,
        )
    )
    return {row[0]: tuple(row[1:]) for row in rows.tuples()}


async def identity_tables_missing(session: AsyncSession) -> list[str]:
    """The tables of migration 20261021_0012 this database lacks (PostgreSQL)."""
    from app.models.identity import GUARDED_TABLES

    if session.get_bind().dialect.name != "postgresql":
        return []
    present = set(
        (
            await session.scalars(
                text(
                    "SELECT c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = current_schema() AND c.relname = ANY(:tables)"
                ),
                {"tables": list(GUARDED_TABLES)},
            )
        ).all()
    )
    return [table for table in GUARDED_TABLES if table not in present]


async def verify_identities(session: AsyncSession) -> tuple[int, list[str], list[str]]:
    """Aliases, supersession links, source chains, devices and decisions are consistent.

    Returns (aliases, errors, notes). Besides the key checks: no links form a cycle (in any
    state), no link supersedes a record that carries its own Google id, every link joins two
    rows of one v2 key, and its two recorded fingerprints - and the rows' forms now - are one
    or were joined by recorded chain links (at any time: a later link may have given the
    holder a newer form); every identity-merged event still has its link. Every chain was
    decided by the reconciler on evidence that satisfies the rule (or by the owner, with the
    decision row), both its fingerprints have sightings, and those never narrowed since (they
    only widen); the live chains never fork or cycle; and ``raw_source_devices`` - what
    readers count by - says for every fingerprint the device its live chains make. Notes, not
    errors: chains rejected or refuted (their links no longer hide anything), links no longer
    in force, live superseded rows whose holders were marked deleted (readers count them
    again), and rows whose holder is itself superseded.
    """
    from app.models.identity import DECIDERS
    from app.services.identity_backfill import spec_for
    from app.services.raw_history import PROTECTIVE_TRIGGERS
    from app.services.source_chains import owner_decision_of, parse_evidence

    notes: list[str] = []
    missing = await identity_tables_missing(session)
    if missing:
        revision = None
        try:
            async with session.begin_nested():
                revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        except Exception:  # noqa: BLE001 - the revision is only for the message
            revision = None
        return (
            0,
            [
                f"the database is at revision {revision or 'unknown'}, without the record "
                f"identity tables of 20261021_0012 ({', '.join(missing)}): run `alembic upgrade "
                "head` (docs/RUNBOOK.md, 20261021_0012) before verifying with this version"
            ],
            notes,
        )
    guarded = {table for table, _, _ in PROTECTIVE_TRIGGERS}
    errors: list[str] = await verify_identity_schema(session, guarded)
    count = await session.scalar(select(func.count()).select_from(RawHealthDataIdentity)) or 0
    for data_type, key in (await session.execute(_ALIAS_MISMATCH_SQL)).tuples():
        errors.append(f"identity alias {data_type}/{key} does not match its raw row")
    for (raw_id,) in (await session.execute(_LINK_MISMATCH_SQL)).tuples():
        errors.append(f"supersession of raw row {raw_id} does not match its rows")
    for (raw_id,) in (await session.execute(_LINK_CYCLE_SQL)).tuples():
        errors.append(f"supersession of raw row {raw_id} forms a cycle")
    for (raw_id,) in (await session.execute(_MERGED_WITHOUT_LINK_SQL)).tuples():
        errors.append(
            f"raw row {raw_id} has an identity-merged event but no supersession link "
            "(the supersession table lost rows or was replaced)"
        )
    counted_again = await session.scalar(_COUNTED_AGAIN_SQL) or 0
    if counted_again:
        notes.append(
            f"{counted_again} superseded row(s) count again: every row that held each was "
            "marked deleted at the source while the row itself is live"
        )

    recorded = await _chain_rows(session)
    decisions = await _decision_rows(session)
    chains: dict[str, Chains] = {}
    for data_type in sorted(
        {*recorded, *(row[0] for row in decisions.values()), *await _device_types(session)}
    ):
        chains[data_type] = await _verify_chains(session, data_type)
    old_row, new_row = aliased(RawHealthData), aliased(RawHealthData)
    link = RawHealthDataSupersession
    links = (
        select(
            link.old_raw_id,
            link.new_raw_id,
            link.data_type,
            link.old_fingerprint,
            link.new_fingerprint,
            old_row.raw_json,
            new_row.raw_json,
            old_row.source_deleted_at,
            new_row.source_deleted_at,
        )
        .join(old_row, old_row.id == link.old_raw_id)
        .join(new_row, new_row.id == link.new_raw_id)
    )
    differing = void = 0
    # Rows superseded in force, and the rows they point at (a holder superseded itself).
    superseded_rows: set[uuid.UUID] = set()
    targets: set[uuid.UUID] = set()
    result = await session.stream(links.execution_options(yield_per=1_000))
    async for row in result:
        raw_id, holder_id, data_type, old_source, new_source = row[:5]
        old_json, new_json, old_deleted, new_deleted = row[5:]
        if google_name(old_json) is not None:
            errors.append(f"supersession of raw row {raw_id} hides a record with a Google id")
        spec = spec_for(data_type)
        old, new = record_identity(spec, old_json), record_identity(spec, new_json)
        if old is None or new is None or old.key != new.key:
            errors.append(f"supersession of raw row {raw_id} links two different records")
            continue
        current = chains.get(data_type) or Chains()
        if not (
            current.ever_joined(old_source, new_source)
            and current.ever_joined(old.source, old_source)
            and current.ever_joined(new.source, new_source)
        ):
            errors.append(f"supersession of raw row {raw_id} links two different sources")
        if not current.in_force(old_source, new_source):
            void += 1
            continue
        superseded_rows.add(raw_id)
        targets.add(holder_id)
        if (
            old_deleted is None
            and new_deleted is None
            and reading(spec, old_json) != reading(spec, new_json)
        ):
            differing += 1
    if differing:
        notes.append(
            f"{differing} live superseded row(s) hold a value their holder no longer has (an "
            "older value of the record; the next complete fetch of their window marks them)"
        )
    if void:
        notes.append(
            f"{void} supersession(s) are no longer in force (their two sources are no longer one "
            "device: a chain was rejected or refuted, or the owner decided them distinct); the "
            "rows they linked count on their own"
        )
    stacked = len(targets & superseded_rows)
    if stacked:
        notes.append(
            f"{stacked} holder row(s) are superseded themselves (readers follow both links)"
        )

    for data_type, current in sorted(chains.items()):
        for problem in current.problems():
            errors.append(f"source chains of {data_type}: {problem} (a chain never forks)")
        wrong = current.devices_to_log()
        if wrong:
            errors.append(
                f"raw_source_devices of {data_type} does not match its live chains for "
                f"{len(wrong)} source(s) (e.g. {sorted(wrong)[0][:12]}): readers would count "
                "supersessions wrongly"
            )
    for data_type, rows in sorted(recorded.items()):
        current = chains[data_type]
        for row in rows:
            name = f"source chain of {data_type} {row.old[:12]} -> {row.new[:12]}"
            if row.decided_by not in DECIDERS:
                errors.append(f"{name} was not decided by the reconciler ({row.decided_by!r})")
            owner = row.decided_by == "owner"
            decided = parse_evidence(row.evidence, owner=owner)
            if decided is None:
                errors.append(f"{name} has no evidence that satisfies the chain rule")
            if owner:
                decision = decisions.get(owner_decision_of(row.evidence) or -1)
                if decision != (data_type, "link", row.old, row.new):
                    errors.append(f"{name} names no owner decision to link them")
            before, after = current.spans.get(row.old), current.spans.get(row.new)
            if before is None or after is None:
                errors.append(f"{name} has no sightings for both sources (they were removed)")
                continue
            name = f"source chain of {data_type} {before.label!r} -> {after.label!r}"
            if decided is not None and (
                before.first > decided[0].first
                or before.last < decided[0].last
                or after.first > decided[1].first
                or after.last < decided[1].last
            ):
                errors.append(f"{name}: the sightings narrowed since it was decided")
            if current.rejected(row.old, row.new):
                notes.append(
                    f"{name} was rejected by the owner: it is no longer used, and the rows it "
                    "linked count on their own unless another chain joins their sources"
                )
            elif current.refuted(row.old, row.new):
                notes.append(
                    f"{name} is refuted (two devices: different values for one record, or the "
                    "owner's decision): it is no longer used, and the rows it linked count on "
                    "their own"
                )
    for decision_id, (data_type, _, first, second) in sorted(decisions.items()):
        spans = chains[data_type].spans
        if first not in spans or second not in spans:
            errors.append(
                f"owner decision {decision_id} of {data_type} names a source without sightings"
            )
    return count, errors, notes


async def _device_types(session: AsyncSession) -> list[str]:
    from app.models.identity import RawSourceDevice

    return list((await session.scalars(select(RawSourceDevice.data_type).distinct())).all())


async def _verify_chains(session: AsyncSession, data_type: str) -> Chains:
    from app.services.source_chains import load_chains

    return await load_chains(session, data_type)


# The constraints of migration 20261021_0012, as pg_get_constraintdef prints them.
EXPECTED_CONSTRAINTS = {
    "raw_health_data_identity": {
        "pk_raw_health_data_identity": "PRIMARY KEY (data_type, identity_key)",
        "fk_raw_health_data_identity_raw_id_raw_health_data": (
            "FOREIGN KEY (raw_id) REFERENCES raw_health_data(id)"
        ),
    },
    "raw_health_data_supersessions": {
        "pk_raw_health_data_supersessions": "PRIMARY KEY (old_raw_id, new_raw_id)",
        "ck_raw_health_data_supersessions_distinct_rows": "CHECK ((old_raw_id <> new_raw_id))",
        "fk_raw_health_data_supersessions_old_raw_id_raw_health_data": (
            "FOREIGN KEY (old_raw_id) REFERENCES raw_health_data(id)"
        ),
        "fk_raw_health_data_supersessions_new_raw_id_raw_health_data": (
            "FOREIGN KEY (new_raw_id) REFERENCES raw_health_data(id)"
        ),
    },
    "raw_source_chains": {
        "pk_raw_source_chains": "PRIMARY KEY (id)",
        "uq_raw_source_chains_link": "UNIQUE (data_type, from_fingerprint, to_fingerprint)",
        "ck_raw_source_chains_distinct_sources": (
            "CHECK (((from_fingerprint)::text <> (to_fingerprint)::text))"
        ),
    },
    "raw_source_pairs": {
        "pk_raw_source_pairs": (
            "PRIMARY KEY (data_type, first_fingerprint, second_fingerprint, identity_key)"
        ),
        "ck_raw_source_pairs_ordered": (
            "CHECK (((first_fingerprint)::text < (second_fingerprint)::text))"
        ),
    },
    "raw_source_decisions": {
        "pk_raw_source_decisions": "PRIMARY KEY (id)",
        "ck_raw_source_decisions_distinct_sources": (
            "CHECK (((first_fingerprint)::text <> (second_fingerprint)::text))"
        ),
        "ck_raw_source_decisions_known_decision": (
            "CHECK (((decision)::text = ANY ((ARRAY['link'::character varying, "
            "'reject'::character varying, 'confirm'::character varying, "
            "'distinct'::character varying, 'not-distinct'::character varying])::text[])))"
        ),
    },
    "raw_source_chain_events": {
        "pk_raw_source_chain_events": (
            "PRIMARY KEY (data_type, from_fingerprint, to_fingerprint, event)"
        ),
    },
    "raw_source_devices": {
        "pk_raw_source_devices": "PRIMARY KEY (id)",
    },
    "raw_source_sightings": {
        "pk_raw_source_sightings": "PRIMARY KEY (data_type, fingerprint)",
    },
}
# Keys and checks only: NOT NULL constraints (pg_constraint lists them from PostgreSQL 18 on)
# and triggers are verified elsewhere.
_CONSTRAINTS_SQL = text(
    "SELECT c.relname, k.conname, pg_get_constraintdef(k.oid), k.convalidated, k.condeferrable "
    "FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = current_schema() AND c.relname = ANY(:tables) "
    "AND k.contype IN ('p', 'u', 'c', 'f', 'x')"
)
_RULES_SQL = text(
    "SELECT tablename, rulename FROM pg_rules "
    "WHERE schemaname = current_schema() AND tablename = ANY(:tables)"
)
_VIEW_COLUMNS_SQL = text(
    "SELECT c.relname, a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = current_schema() AND c.relkind = 'v' AND c.relname = ANY(:views) "
    "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY c.relname, a.attnum"
)
# Row-level security on a guarded table can hide rows from readers and verify alike.
_ROW_SECURITY_SQL = text(
    "SELECT c.relname, c.relkind, c.relrowsecurity, c.relforcerowsecurity, "
    "(SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = current_schema() AND c.relname = ANY(:tables)"
)
_VIEWS_SQL = text(
    "SELECT c.relname, pg_get_viewdef(c.oid) FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind = 'v' AND c.relnamespace = pg_my_temp_schema() OR "
    "(n.nspname = current_schema() AND c.relname = ANY(:views))"
)


async def verify_identity_schema(session: AsyncSession, guarded: Iterable[str]) -> list[str]:
    """PostgreSQL: the identity tables keep their keys and checks (a dropped primary key
    would let a row be superseded twice, a dropped check let a row supersede itself), no
    rule sits on a guarded table (``DO INSTEAD`` would bypass the triggers), no guarded table
    has row-level security or a policy (it could hide rows), every guarded table is a plain
    table, and the ``analytics_*`` views are exactly the ones of 0012."""
    if session.get_bind().dialect.name != "postgresql":
        return []
    errors: list[str] = []
    tables = sorted(guarded)
    present = set()
    security = await session.execute(_ROW_SECURITY_SQL, {"tables": tables})
    for table, kind, enabled, forced, policies in security.tuples():
        present.add(table)
        kind = kind.decode() if isinstance(kind, bytes) else kind
        if kind != "r":
            errors.append(f"guarded table {table} is not a plain table (relkind {kind})")
        if enabled or forced or policies:
            errors.append(
                f"guarded table {table} has row-level security (enabled={enabled}, "
                f"forced={forced}, policies={policies}); it could hide rows: run ALTER TABLE "
                f"{table} NO FORCE ROW LEVEL SECURITY, DISABLE ROW LEVEL SECURITY and drop "
                "its policies"
            )
    for table in tables:
        if table not in present:
            errors.append(f"guarded table missing: {table}")
    errors.extend(await verify_views(session))
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


async def verify_views(session: AsyncSession) -> list[str]:
    """The ``analytics_*`` views of 0012 were not redefined: each is compared with what this
    server makes of the expected definition (a temporary view, in a rolled-back savepoint).

    ``SELECT *`` is expanded when a view is created, so a column added to a normalized table
    later is not in its view: the expected definition selects the view's own columns, and
    only the conditions (what is counted) must match."""
    from app.services.analytics_views import NORMALIZED_TABLES, view_select

    errors: list[str] = []
    views = [f"analytics_{table}" for table in NORMALIZED_TABLES]
    columns: dict[str, list[str]] = defaultdict(list)
    for view, column in (await session.execute(_VIEW_COLUMNS_SQL, {"views": views})).tuples():
        columns[view].append(column)
    savepoint = await session.begin_nested()
    try:
        for table in NORMALIZED_TABLES:
            expected = view_select(table)
            own = columns.get(f"analytics_{table}")
            if own:
                listed = ", ".join(f'{table}."{column}"' for column in own)
                expected = expected.replace(
                    f"SELECT * FROM {table}", f"SELECT {listed} FROM {table}", 1
                )
            try:
                async with session.begin_nested():
                    await session.execute(
                        text(f"CREATE TEMP VIEW healthos_expected_{table} AS {expected}")
                    )
            except Exception:  # noqa: BLE001 - a column the table lacks: redefined
                errors.append(
                    f"analytics view analytics_{table} was redefined: its columns are not "
                    f"columns of {table} (recreate it from migration 20261021_0012)"
                )
        found = dict((await session.execute(_VIEWS_SQL, {"views": views})).tuples().all())
    finally:
        await savepoint.rollback()
    for table in NORMALIZED_TABLES:
        actual = found.get(f"analytics_{table}")
        expected = found.get(f"healthos_expected_{table}")
        if actual is None:
            errors.append(f"analytics view missing: analytics_{table}")
        elif expected is not None and " ".join(actual.split()) != " ".join(expected.split()):
            errors.append(
                f"analytics view analytics_{table} was redefined: it no longer counts what the "
                "dashboard counts (recreate it from migration 20261021_0012)"
            )
    return errors
