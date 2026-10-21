"""Resolve records without a Google id to the raw row that holds them (v2 identities).

``resolve_ids`` decides, for one page of records, under which ``google_data_point_id`` each
is written. A record with a Google ``name`` keeps it. The others are grouped by v2 key
(app/parsers/identity.py) and source fingerprint. The write path never *decides* whether two
fingerprints are one device; it only applies what the reconciler decided
(app/services/source_chains.py). Every group is written onto

1. a row that holds this key from this *same* fingerprint (a returning record, civil times
   added, a grown interval) - the row that holds it now when the row was merged into another
   of the same fingerprint; or
2. else, along a decided, never contradicted chain, the row of a *predecessor* fingerprint
   (a renamed device, a dropped package suffix) whose reading is the same - it gets the new
   payload as a new version, so nothing is archived twice; or
3. else a new row ``synthetic:v2s:<hash of key and fingerprint>``: an unknown fingerprint
   always gets rows of its own. A renamed device is then counted twice until the reconciler
   links the rows (nightly); a genuinely distinct device is never hidden.

Stored rows are found by id only (no scan): the record's v1 id (rows archived before v2), the
v2s id of the key and fingerprint (rows stored since), and the alias table (rows whose own id
is neither, aliased by the reconciler). A payload of one fingerprint is never written onto a
row that holds another fingerprint, except by rule 2. ``store_records`` locks the rows it
writes onto and re-resolves when one changed meanwhile, and creates new rows with ``ON
CONFLICT DO NOTHING`` (re-resolving when another writer created the row first).

``present_records`` decides, for deletion marking, whether an unseen row's record is still
returned under another row: by its own fingerprint or a decided chain (present), by a
fingerprint not yet decided distinct from it (deferred: possibly a rename the reconciler has
not decided yet), or not at all (missing).
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
from app.parsers.common import source_id
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
    load_chains,
    lock_data_type,
    record_contradictions,
    record_pairs,
    record_sightings,
)

logger = logging.getLogger(__name__)

# Supersession links are followed at most this far (the reconciler never links to a row that
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
    # Records written onto a predecessor fingerprint's row along a decided chain.
    chained: int = 0


@dataclass
class _Group:
    """The records of one page with one v2 key and one source fingerprint."""

    key: str
    source: str
    identity: RecordIdentity
    indexes: list[int] = field(default_factory=list)
    readings: set[str | None] = field(default_factory=set)


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
    reading) of the page's records; a row holding exactly such a payload is not parsed."""
    found: dict[uuid.UUID, StoredRow] = {}
    for chunk in _chunks(sorted(raw_ids)):
        rows = await session.execute(select(*_LIGHT).where(among(session, RawHealthData.id, chunk)))
        for raw_id, google_id, digest, seen, deleted in rows.tuples():
            row = StoredRow(raw_id, google_id, digest, as_utc(seen), deleted is not None)
            if digest in known:
                row.identity, row.reading = known[digest]
            found[raw_id] = row
    unparsed = sorted(raw_id for raw_id, row in found.items() if row.identity is None)
    for chunk in _chunks(unparsed):
        rows = await session.execute(
            select(RawHealthData.id, RawHealthData.raw_json).where(
                among(session, RawHealthData.id, chunk)
            )
        )
        for raw_id, raw_json in rows.tuples():
            found[raw_id].identity = record_identity(spec, raw_json)
            found[raw_id].reading = reading(spec, raw_json)
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
    seen: dict[str, tuple[str, str, object]] = {}
    known: dict[str, tuple] = {}
    for index, record in enumerate(records):
        identity = record_identity(spec, record)
        if identity is None:
            continue
        assert isinstance(record, dict)
        data_source = record.get("dataSource")
        seen.setdefault(identity.source, (identity.stable, source_label(data_source), data_source))
        group = groups.setdefault(
            (identity.key, identity.source), _Group(identity.key, identity.source, identity)
        )
        group.indexes.append(index)
        value = reading(spec, record)
        group.readings.add(value)
        known[digests[index]] = (identity, value)
    if not groups:
        return resolution

    await lock_data_type(session, spec.name, exclusive=False)
    # The page's sources are seen now (the database clock).
    now = await record_sightings(session, spec.name, seen)
    chains = await load_chains(session, spec.name, seen)
    for old, new in await record_contradictions(session, spec.name, chains, now):
        event(
            logger,
            "SOURCE_CHAIN_CONTRADICTED",
            data_type=spec.name,
            old=chains.spans[old].label,
            new=chains.spans[new].label,
        )
    earlier = {fingerprint: chains.predecessors(fingerprint) for fingerprint in seen}
    sources_by_key: dict[str, list[str]] = defaultdict(list)
    for key, source in groups:
        sources_by_key[key].append(source)
    # Two sources in one page for one key: two distinct devices (a record has one source).
    await record_pairs(
        session,
        spec.name,
        [
            (left, right)
            for sources in sources_by_key.values()
            for index, left in enumerate(sources)
            for right in sources[index + 1 :]
        ],
        now,
    )

    wanted_keys: set[str] = set()
    wanted_ids: set[str] = set()
    for group in groups.values():
        for source in (group.source, *earlier[group.source]):
            name = specific_key(group.key, source)
            wanted_keys.add(name)
            wanted_ids.add(new_record_id(name))
        wanted_ids.update(ids[index] for index in group.indexes)
    alias = await aliases(session, spec.name, wanted_keys)
    by_id = await rows_by_key(session, spec.name, wanted_ids)
    found = set(alias.values()) | set(by_id.values())
    holders = await successors(session, found)
    rows = await _load_rows(session, spec, found | set(holders.values()), known)
    for raw_id, holder in holders.items():
        rows[raw_id].holder = holder
    for row in rows.values():
        if row.holder is None:
            row.holder = row.raw_id

    by_identity: dict[tuple[str, str], list[StoredRow]] = defaultdict(list)
    for row in rows.values():
        if row.identity is not None:
            by_identity[(row.identity.key, row.identity.source)].append(row)

    targets: dict[tuple[str, str], StoredRow] = {}
    # Own rows first: a row of the group's own fingerprint is never taken by a chain.
    for name in sorted(groups):
        own = by_identity.get(name, [])
        current = [row for row in own if row.holder == row.raw_id]
        if current:
            targets[name] = max(current, key=lambda row: row.order)
        elif own:
            # Merged by the reconciler into a row of another fingerprint, and that old form
            # was returned again: the record goes back onto its own row (the merge stays).
            targets[name] = max(own, key=lambda row: row.order)
    claimed = {row.raw_id for row in targets.values()}
    # Then decided chains: the row of the closest earlier fingerprint, same reading.
    for (key, source), group in sorted(groups.items()):
        if (key, source) in targets:
            continue
        for previous in earlier[source]:
            matches = [
                row
                for row in by_identity.get((key, previous), [])
                if row.holder == row.raw_id
                and row.raw_id not in claimed
                and group.readings == {row.reading}
            ]
            if matches:
                target = max(matches, key=lambda row: row.order)
                targets[(key, source)] = target
                claimed.add(target.raw_id)
                resolution.chained += len(group.indexes)
                break
    fresh: list[tuple[_Group, str]] = []
    for name, group in sorted(groups.items()):
        own_key = group.identity.specific_key
        target = targets.get(name)
        if target is None:
            fresh.append((group, new_record_id(own_key)))
            continue
        resolution.expected[target.raw_id] = target.payload_hash
        for index in group.indexes:
            ids[index] = target.google_data_point_id
        if target.google_data_point_id != new_record_id(own_key) and own_key not in alias:
            resolution.new_aliases[own_key] = target.google_data_point_id
    if fresh:
        await _new_ids(session, spec, fresh, by_id, ids, resolution)
    return resolution


async def _new_ids(
    session: AsyncSession,
    spec: DataTypeSpec,
    fresh: list[tuple[_Group, str]],
    by_id: dict[str, uuid.UUID],
    ids: list[str],
    resolution: Resolution,
) -> None:
    """A new row's id must not be one a stored row already has (a chain may have written
    another fingerprint's payload onto the row of that id): such a record gets a spare id."""
    for group, target in fresh:
        if target in by_id:
            taken = await rows_by_key(
                session,
                spec.name,
                [spare_record_id(group.identity, attempt) for attempt in range(1, 9)],
            )
            for attempt in range(1, 9):
                candidate = spare_record_id(group.identity, attempt)
                if candidate not in taken and candidate not in resolution.created:
                    target = candidate
                    break
            else:  # pragma: no cover - eight rows of one record and fingerprint
                raise RuntimeError(f"no free id for a record of {spec.name}")
            resolution.new_aliases.setdefault(group.identity.specific_key, target)
        resolution.created.add(target)
        for index in group.indexes:
            ids[index] = target


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
    for offset in range(0, len(values), 1000):
        await session.execute(statement, values[offset : offset + 1000])
    return len(values)


@dataclass
class Presence:
    # Candidates whose record a row of the same device was seen holding.
    present: set[uuid.UUID] = field(default_factory=set)
    # Candidates whose key a source not yet decided distinct was seen with (a possible
    # rename the reconciler has not decided): not marked now.
    deferred: set[uuid.UUID] = field(default_factory=set)


async def present_records(
    session: AsyncSession,
    spec: DataTypeSpec,
    candidates: Sequence[tuple[uuid.UUID, object, datetime | None]],
    seen_since: datetime,
) -> Presence:
    """Candidates (raw id, raw_json, stored start) that must not be marked deleted.

    For each candidate, the rows with its v2 key seen at or after ``seen_since`` decide:

    * one of the *same device* (the same fingerprint, or one linked to it by a usable chain)
      - a duplicate of the record under another id: present;
    * else one of another fingerprint that is not decided distinct from the candidate's (the
      two were never returned together for one key, so it may be the candidate renamed, or
      its old name come back): deferred, until the reconciler has decided;
    * else (none, or only fingerprints once returned together with the candidate's): missing.
      Of two devices that recorded at one instant, one can be deleted alone.
    """
    presence = Presence()
    identities = {raw_id: record_identity(spec, raw_json) for raw_id, raw_json, _ in candidates}
    starts = {identity.start for identity in identities.values() if identity and identity.start}
    # A stale stored start (written by an older parser) still finds the other rows.
    starts |= {as_utc(start) for _, _, start in candidates if start is not None}
    if not starts:
        return presence
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
    # Returned together for one key in this fetch: distinct, from now on too.
    together = {
        (left, right)
        for found in seen_rows.values()
        for left in found
        for right in found
        if left < right
    }
    if together - chains.together:
        from app.services.source_chains import database_now

        await record_pairs(session, spec.name, together, await database_now(session))
        chains.together |= together
    for raw_id, identity in identities.items():
        if identity is None:
            continue
        others = seen_rows.get(identity.key, set())
        if any(chains.same_device(identity.source, other) for other in others):
            presence.present.add(raw_id)
        elif any(not chains.distinct(identity.source, other) for other in others):
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
        )
    )
    for data_type, old, new, decided_by, evidence, created in rows.tuples():
        found[data_type].append(_ChainRow(old, new, decided_by, evidence, as_utc(created)))
    return found


def _linked_when(rows: list[_ChainRow], left: str, right: str, at: datetime) -> bool:
    """``left`` and ``right`` were one chain (either way) by the links recorded until ``at``."""
    following = {row.old: row.new for row in rows if row.created_at <= at}
    for start, target in ((left, right), (right, left)):
        current = start
        for _ in range(64):
            current = following.get(current, "")
            if not current:
                break
            if current == target:
                return True
    return False


async def verify_identities(session: AsyncSession) -> tuple[int, list[str], list[str]]:
    """Aliases, supersession links and source chains are consistent with their rows.

    Returns (aliases, errors, notes). Besides the key checks: no link forms a cycle or
    points at a superseded row, no link hides a record with a Google id or a live record
    behind a deleted one, every link joins two rows of one v2 key and either one fingerprint
    or two that a chain recorded *before the link was made* joins, and every identity-merged
    event still has its link. Every chain was decided by the reconciler on evidence that
    satisfies the rule, both its fingerprints have sightings, and those never narrowed
    since (they only widen). A chain its sightings contradict today (the old form seen again)
    is a note: it is no longer used for new pairings, and the rows it paired kept every
    payload as a version.
    """
    from app.models.identity import DECIDERS
    from app.services.identity_backfill import spec_for
    from app.services.raw_history import PROTECTIVE_TRIGGERS
    from app.services.source_chains import parse_evidence

    notes: list[str] = []
    guarded = {table for table, _, _ in PROTECTIVE_TRIGGERS}
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
    for (raw_id,) in (await session.execute(_MERGED_WITHOUT_LINK_SQL)).tuples():
        errors.append(
            f"raw row {raw_id} has an identity-merged event but no supersession link "
            "(the supersession table lost rows or was replaced)"
        )
    for data_type, fingerprint in (await session.execute(_CHAIN_CYCLE_SQL)).tuples():
        errors.append(f"source chain of {data_type} from {fingerprint[:12]} forms a cycle")

    recorded = await _chain_rows(session)
    chains: dict[str, Chains] = {}
    for data_type in sorted(recorded):
        chains[data_type] = await load_chains(session, data_type)
    old_row, new_row = aliased(RawHealthData), aliased(RawHealthData)
    links = (
        select(
            RawHealthDataSupersession.old_raw_id,
            RawHealthDataSupersession.data_type,
            RawHealthDataSupersession.created_at,
            old_row.raw_json,
            new_row.raw_json,
        )
        .join(old_row, old_row.id == RawHealthDataSupersession.old_raw_id)
        .join(new_row, new_row.id == RawHealthDataSupersession.new_raw_id)
    )
    result = await session.stream(links.execution_options(yield_per=1_000))
    async for raw_id, data_type, created, old_json, new_json in result:
        spec = spec_for(data_type)
        old, new = record_identity(spec, old_json), record_identity(spec, new_json)
        if old is None or new is None or old.key != new.key:
            errors.append(f"supersession of raw row {raw_id} links two different records")
            continue
        if old.source != new.source and not _linked_when(
            recorded.get(data_type, []), old.source, new.source, as_utc(created)
        ):
            errors.append(f"supersession of raw row {raw_id} links two different sources")

    for data_type, rows in sorted(recorded.items()):
        current = chains[data_type]
        for row in rows:
            name = f"source chain of {data_type} {row.old[:12]} -> {row.new[:12]}"
            if row.decided_by not in DECIDERS:
                errors.append(f"{name} was not decided by the reconciler ({row.decided_by!r})")
            decided = parse_evidence(row.evidence)
            if decided is None:
                errors.append(f"{name} has no evidence that satisfies the chain rule")
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
            if current.contradicts(row.old, row.new):
                notes.append(
                    f"{name} is contradicted by later sightings (the old form was seen again): "
                    "it is no longer used for new pairings; the rows it linked stay linked"
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
    "raw_source_pairs": {
        "pk_raw_source_pairs": "PRIMARY KEY (data_type, first_fingerprint, second_fingerprint)",
        "ck_raw_source_pairs_ordered": (
            "CHECK (((first_fingerprint)::text < (second_fingerprint)::text))"
        ),
    },
    "raw_source_chain_events": {
        "pk_raw_source_chain_events": "PRIMARY KEY (data_type, from_fingerprint, event)",
    },
    "raw_source_sightings": {
        "pk_raw_source_sightings": "PRIMARY KEY (data_type, fingerprint)",
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
    server makes of the expected definition (a temporary view, in a rolled-back savepoint)."""
    from app.services.analytics_views import NORMALIZED_TABLES, view_select

    errors: list[str] = []
    views = [f"analytics_{table}" for table in NORMALIZED_TABLES]
    savepoint = await session.begin_nested()
    try:
        for table in NORMALIZED_TABLES:
            await session.execute(
                text(f"CREATE TEMP VIEW healthos_expected_{table} AS {view_select(table)}")
            )
        found = dict((await session.execute(_VIEWS_SQL, {"views": views})).tuples().all())
    finally:
        await savepoint.rollback()
    for table in NORMALIZED_TABLES:
        actual = found.get(f"analytics_{table}")
        expected = found.get(f"healthos_expected_{table}")
        if actual is None:
            errors.append(f"analytics view missing: analytics_{table}")
        elif " ".join(actual.split()) != " ".join((expected or "").split()):
            errors.append(
                f"analytics view analytics_{table} was redefined: it no longer counts what the "
                "dashboard counts (recreate it from migration 20261021_0012)"
            )
    return errors
