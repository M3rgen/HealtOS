"""Resolve records without a Google id to the raw row that holds them (v2 identities).

``resolve_ids`` decides, for one page of records, under which ``google_data_point_id`` each
is written. A record with a Google ``name`` keeps it. The others are grouped by v2 key
(app/parsers/identity.py) and source fingerprint. **A payload is only ever written onto a row
of its own fingerprint** (its v1 row, its ``v2s`` row or one of its spare rows): a row's
fingerprint never changes, so no row's history holds another source's payload. Whether two
fingerprints are one device is decided elsewhere (app/services/source_chains.py: by the
reconciler or the owner); de-duplication across fingerprints is a matter of supersession links
alone, which readers apply while they are *in force* (their two fingerprints one device).

A row is *hidden* exactly when readers skip it (``held_elsewhere``): it has a link in force to
a row that is neither marked deleted nor quarantined. Every group is written onto

1. its fingerprint's *root* row (one not superseded by a link of the same fingerprint; the
   one not hidden first, then the live one, then the one seen last) when that row is not
   hidden, or when the rows hiding it show the same reading (the record is counted there);
2. else a new row of its own (``synthetic:v2s:<hash of key and fingerprint>``, or the first
   free *spare* id of the record and fingerprint when that id is taken): a reading Google
   returns is never hidden behind a row showing another value.

A new row is linked at once to a row of the same device (another fingerprint joined to its own
by live chain links) that is counted and shows the same reading, so a decided rename is never
counted twice; with no such row it counts on its own (an unknown fingerprint, a second device,
a revised value), never hidden. Stored rows are found by id only: the record's v1 id, its
``v2s`` id, its spare ids (in blocks, when its ``v2s`` row is not a counted root) and its alias;
the same for the other fingerprints of its device. ``store_records`` locks the rows it writes
onto or links to and re-resolves when one changed meanwhile.

``present_records`` decides, for deletion marking, whether an unseen row's record is still
returned: with the same reading under its own device (present); with the same reading under a
fingerprint not *certainly* another device (deferred: possibly a rename nobody decided yet, or
reprocessed records returned under a new name); or not at all, or only with another value
(missing).
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import (
    String,
    any_,
    bindparam,
    func,
    insert,
    literal,
    select,
    text,
    union_all,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.timeutil import as_utc
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
    supersession_reason,
)
from app.services.source_chains import (
    Chains,
    SourceState,
    load_chains,
    load_state,
    lock_data_type,
    ordered_pair,
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


_LIGHT = (
    RawHealthData.id,
    RawHealthData.google_data_point_id,
    RawHealthData.payload_hash,
    RawHealthData.last_seen_at,
    RawHealthData.source_deleted_at,
    RawHealthData.is_quarantined,
)


class _Rows:
    """The stored rows a page needs, with their identity, reading and direct links."""

    def __init__(
        self,
        session: AsyncSession,
        spec: DataTypeSpec,
        known: dict[str, tuple],
        named: dict[str, RecordIdentity],
    ):
        self.session, self.spec, self.known = session, spec, known
        # The record and source a row with one of these ids holds (its v2s and spare ids: a
        # row's source never changes): a deleted one is not read whole (it hides nothing, and
        # what it showed matters to no one).
        self.named = named
        self.rows: dict[uuid.UUID, StoredRow] = {}
        # v2 key -> its rows (every source).
        self.of_key: dict[str, list[StoredRow]] = defaultdict(list)
        # google id -> raw id, alias key -> raw id (of what was looked up and found).
        self.by_id: dict[str, uuid.UUID] = {}
        self.alias: dict[str, uuid.UUID] = {}
        self.looked_up: set[str] = set()
        self._incomplete: set[uuid.UUID] = set()

    def _add(self, values: tuple) -> StoredRow:
        """A row by its light columns; its payload is read later (``complete``) only when this
        page does not carry the same payload and the row may matter."""
        from app.core.timeutil import as_utc

        raw_id, google_id, digest, seen, deleted, quarantined = values
        row = self.rows.get(raw_id)
        if row is not None:
            return row
        row = StoredRow(
            raw_id, google_id, digest, as_utc(seen), deleted is not None, bool(quarantined)
        )
        self.rows[raw_id] = row
        if digest in self.known:
            row.identity, row.reading = self.known[digest][:2]
        elif row.deleted and google_id in self.named:
            row.identity = self.named[google_id]
        else:
            self._incomplete.add(raw_id)
            return row
        if row.identity is not None:
            self.of_key[row.identity.key].append(row)
        return row

    async def complete(self) -> None:
        """Read the payloads the identities and readings still need."""
        wanted, self._incomplete = sorted(self._incomplete), set()
        for chunk in _chunks(wanted):
            result = await self.session.execute(
                select(RawHealthData.id, RawHealthData.raw_json).where(
                    among(self.session, RawHealthData.id, chunk)
                )
            )
            for raw_id, raw_json in result.tuples():
                row = self.rows[raw_id]
                row.identity = record_identity(self.spec, raw_json)
                row.reading = reading(self.spec, raw_json)
                row.raw_json = raw_json
                if row.identity is not None:
                    self.of_key[row.identity.key].append(row)

    def holding(self, name: tuple[str, str]) -> list[StoredRow]:
        """The rows of the record ``name`` (v2 key, fingerprint)."""
        return [row for row in self.of_key.get(name[0], ()) if _holds(row, name)]

    def payload(self, row: StoredRow) -> object:
        return row.raw_json if row.raw_json is not None else self.known[row.payload_hash][2]

    async def find(self, google_ids: Iterable[str], keys: Iterable[str] = ()) -> None:
        """Rows by id and by alias, in one statement per chunk, then the payloads needed."""
        google_ids, keys = set(google_ids) - self.looked_up, set(keys) - self.looked_up
        self.looked_up |= google_ids | keys
        ids_left, keys_left = sorted(google_ids), sorted(keys)
        table = RawHealthDataIdentity
        while ids_left or keys_left:
            id_chunk, ids_left = ids_left[:_CHUNK], ids_left[_CHUNK:]
            key_chunk, keys_left = keys_left[:_CHUNK], keys_left[_CHUNK:]
            parts = []
            if id_chunk:
                parts.append(
                    select(literal("r", String), RawHealthData.google_data_point_id, *_LIGHT).where(
                        RawHealthData.data_type == self.spec.name,
                        among(self.session, RawHealthData.google_data_point_id, id_chunk),
                    )
                )
            if key_chunk:
                parts.append(
                    select(literal("a", String), table.identity_key, *_LIGHT)
                    .join(RawHealthData, RawHealthData.id == table.raw_id)
                    .where(
                        table.data_type == self.spec.name,
                        among(self.session, table.identity_key, key_chunk),
                    )
                )
            statement = union_all(*parts) if len(parts) > 1 else parts[0]
            for kind, name, *values in (await self.session.execute(statement)).tuples():
                row = self._add(tuple(values))
                (self.alias if kind == "a" else self.by_id)[name] = row.raw_id
        await self.complete()

    async def load_links(self, raw_ids: Iterable[uuid.UUID]) -> None:
        """Every direct link of these rows (any state: a deleted holder may come back, a link
        not in force may be again) and, transitively, those of the rows they point at; each
        such row is loaded once."""
        link = RawHealthDataSupersession
        frontier = sorted({raw_id for raw_id in raw_ids if self.rows[raw_id].links is None})
        for _ in range(MAX_CHAIN):
            if not frontier:
                return
            for raw_id in frontier:
                self.rows[raw_id].links = []
            missing: set[uuid.UUID] = set()
            for chunk in _chunks(frontier):
                statement = select(
                    link.old_raw_id, link.new_raw_id, link.old_fingerprint, link.new_fingerprint
                ).where(among(self.session, link.old_raw_id, chunk))
                for old, new, old_source, new_source in (
                    await self.session.execute(statement)
                ).tuples():
                    links = self.rows[old].links
                    assert links is not None
                    links.append((new, old_source, new_source))
                    if new not in self.rows:
                        missing.add(new)
            for chunk in _chunks(sorted(missing)):
                result = await self.session.execute(
                    select(*_LIGHT).where(among(self.session, RawHealthData.id, chunk))
                )
                for values in result.tuples():
                    self._add(tuple(values))
            await self.complete()
            frontier = sorted(
                {
                    new
                    for raw_id in frontier
                    for new, _, _ in self.rows[raw_id].links or ()
                    if self.rows[new].links is None
                }
            )

    def beyond(self, row: StoredRow) -> list[StoredRow]:
        """Every row ``row`` is linked to, directly or through others (any state)."""
        found: dict[uuid.UUID, StoredRow] = {}
        frontier = [row]
        while frontier:
            current = frontier.pop()
            for holder, _, _ in current.links or ():
                target = self.rows.get(holder)
                if target is not None and holder not in found and holder != row.raw_id:
                    found[holder] = target
                    frontier.append(target)
        return list(found.values())


class _Page:
    """What readers will count once the page is written: rows the page writes onto are live
    and show the page's reading."""

    def __init__(self, rows: dict[uuid.UUID, StoredRow], chains: Chains) -> None:
        self.rows, self.chains = rows, chains
        self._claimed: dict[uuid.UUID, _Group] = {}
        # Per decision round (``claimed`` resets them): each row's hiders, and whether a row
        # shows a group's record (rows of a much-linked record are asked again and again).
        self._hiders: dict[uuid.UUID, list[StoredRow]] = {}
        self._shows: dict[tuple[uuid.UUID, tuple[str, str]], bool] = {}

    @property
    def claimed(self) -> dict[uuid.UUID, _Group]:
        return self._claimed

    @claimed.setter
    def claimed(self, value: dict[uuid.UUID, _Group]) -> None:
        self._claimed = value
        self._hiders.clear()
        self._shows.clear()

    def live(self, row: StoredRow) -> bool:
        return not row.quarantined and (row.raw_id in self.claimed or not row.deleted)

    def shown(self, row: StoredRow) -> tuple[str | None, str | None]:
        """What readers see of the row: its reading and, for a growing interval, its end."""
        group = self.claimed.get(row.raw_id)
        if group is not None:
            return group.reading, group.identity.shape
        return row.reading, row.identity.shape if row.identity is not None else None

    def hiders(self, row: StoredRow) -> list[StoredRow]:
        """The rows that hide ``row`` from readers (``held_elsewhere``): targets of its links
        in force that are neither marked deleted nor quarantined."""
        found = self._hiders.get(row.raw_id)
        if found is None:
            found = []
            for holder, old_source, new_source in row.links or ():
                target = self.rows.get(holder)
                if target is not None and self.chains.in_force(old_source, new_source):
                    if self.live(target) and target not in found:
                        found.append(target)
            self._hiders[row.raw_id] = found
        return found

    def hidden(self, row: StoredRow) -> bool:
        return bool(self.hiders(row))

    def counted(self, row: StoredRow) -> bool:
        return self.live(row) and not self.hidden(row)

    def shows(self, row: StoredRow, group: _Group, depth: int = 0) -> bool:
        """Readers count ``group``'s record as Google returns it now through ``row``: the row
        counts and shows its reading (a growing interval: and its end, neither an earlier nor
        a later one), or a row hiding it does."""
        if depth > MAX_CHAIN or not self.live(row):
            return False
        name = (row.raw_id, (group.key, group.source))
        found = self._shows.get(name)
        if found is None:
            self._shows[name] = False  # a cycle of links shows nothing
            hiders = self.hiders(row)
            if not hiders:
                found = self.shown(row) == (group.reading, group.identity.shape)
            else:
                found = any(self.shows(hider, group, depth + 1) for hider in hiders)
            self._shows[name] = found
        return found

    def fits(self, root: StoredRow, group: _Group) -> bool:
        """Writing ``group`` onto ``root`` leaves its reading counted: the root is not hidden,
        or a row hiding it shows the same reading."""
        hiders = self.hiders(root)
        return not hiders or any(self.shows(hider, group) for hider in hiders)


def _holds(row: StoredRow, name: tuple[str, str]) -> bool:
    """The row holds the record ``name`` (v2 key, fingerprint)."""
    return row.identity is not None and (row.identity.key, row.identity.source) == name


def _best(own: list[StoredRow], group: _Group, page: _Page) -> StoredRow | None:
    """The row of the record's own fingerprint the group is written onto: one that leaves its
    reading counted (``fits``), best first - counted, live, the same payload already, not
    superseded by a row of its own fingerprint, seen last. ``None``: every own row would hide the
    reading (behind a row showing another value), so it needs a row of its own."""
    fitting = [row for row in own if page.fits(row, group)]
    if not fitting:
        return None
    return max(
        fitting,
        key=lambda row: (
            not page.hidden(row),
            page.live(row),
            row.payload_hash == group.digest,
            not row.same_source_link(),
            row.last_seen_at,
            str(row.raw_id),
        ),
    )


async def resolve_ids(
    session: AsyncSession,
    spec: DataTypeSpec,
    records: Sequence[object],
    digests: Sequence[str],
) -> Resolution:
    """``digests``: each record's ``payload_hash``, in the same order."""
    from app.services.source_chains import key_time

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
        moment = key_time(identity.key)
        stable, label, source, newest = resolution.seen.get(
            identity.source, (identity.stable, source_label(data_source), data_source, moment)
        )
        if moment is not None and (newest is None or moment > newest):
            newest = moment
        resolution.seen[identity.source] = (stable, label, source, newest)
        group = groups.setdefault(
            (identity.key, identity.source), _Group(identity.key, identity.source, identity)
        )
        group.indexes.append(index)
        group.reading = reading(spec, record)
        group.digest = digests[index]
        group.record = record
        known[digests[index]] = (identity, group.reading, record)
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

    # The record's own rows: by id (its v1 ids, its v2s id), then by alias for the records
    # without a row found so (rows archived before v2 whose v1 id changed).
    named = {
        new_record_id(group.identity.specific_key): group.identity for group in groups.values()
    }
    stored = _Rows(session, spec, known, named)
    google_ids: set[str] = set()
    for group in groups.values():
        google_ids.add(new_record_id(group.identity.specific_key))
        google_ids.update(ids[index] for index in group.indexes)
    await stored.find(google_ids)
    unfound = {
        group.identity.specific_key for name, group in groups.items() if not stored.holding(name)
    }
    if unfound:
        await stored.find(set(), unfound)
    await stored.load_links(stored.rows)
    page = _Page(stored.rows, chains)

    # Which own row each group writes onto. Rows written by this page count with its reading,
    # which may let another group's row show it: decided until nothing changes. A group none
    # of whose rows fits, while its v2s id is taken, may have spare rows (one was made each
    # time its rows were hidden behind another value): found by id, in blocks (spare ids are
    # taken in order, so the first block with a free one is the last), and decided again.
    targets: dict[tuple[str, str], StoredRow | None] = {}
    probed: set[tuple[str, str]] = set()
    while True:
        owned = {name: stored.holding(name) for name in groups}
        targets = {}
        for _ in range(4):
            decided = {
                name: _best(owned[name], group, page) for name, group in sorted(groups.items())
            }
            page.claimed = {row.raw_id: groups[name] for name, row in decided.items() if row}
            if decided == targets:
                break
            targets = decided
        pending = [
            name
            for name, group in groups.items()
            if targets.get(name) is None
            and name not in probed
            and new_record_id(group.identity.specific_key) in stored.by_id
        ]
        if not pending:
            break
        probed.update(pending)
        attempt = 1
        while pending:
            block = range(attempt, attempt + SPARE_BLOCK)
            spares = {
                spare_record_id(groups[name].identity, number): groups[name].identity
                for name in pending
                for number in block
            }
            stored.named.update(spares)
            await stored.find(set(spares))
            pending = [
                name
                for name in pending
                if all(
                    spare_record_id(groups[name].identity, number) in stored.by_id
                    for number in block
                )
            ]
            attempt += SPARE_BLOCK
        await stored.load_links(stored.rows)

    fresh: list[_Group] = []
    revived: list[tuple[_Group, StoredRow]] = []
    for name, group in sorted(groups.items()):
        own_key = group.identity.specific_key
        target = targets.get(name)
        if target is None:
            if stored.holding(name):
                resolution.kept_apart += len(group.indexes)
            fresh.append(group)
            continue
        _expect(resolution, target)
        for row in stored.beyond(target):
            # Every row that could hide it (a deleted one may be revived meanwhile).
            _expect(resolution, row)
        if target.deleted and not page.hidden(target):
            revived.append((group, target))
        # Found by the record's own ids (its v1 id, its v2s id) again next time: no alias.
        own_ids = {new_record_id(own_key), *(ids[index] for index in group.indexes)}
        for index in group.indexes:
            ids[index] = target.google_data_point_id
        if target.google_data_point_id not in own_ids and own_key not in stored.alias:
            resolution.new_aliases[own_key] = target.google_data_point_id
    if fresh:
        _new_ids(fresh, stored.by_id, ids, resolution)
    written: list[tuple[_Group, StoredRow | None]] = [(group, None) for group in fresh]
    written.extend(revived)
    linkable = [item for item in written if len(chains.members(item[0].source)) > 1]
    if linkable:
        # The other fingerprints' rows of a decided device (their v2s ids and aliases): a
        # row this page makes counted (new, or marked deleted until now) holds those that show
        # its reading.
        others: set[str] = set()
        for group, _ in linkable:
            for source in chains.members(group.source) - {group.source}:
                other = specific_key(group.key, source)
                others |= {new_record_id(other), other}
        await stored.find(
            {name for name in others if name.startswith("synthetic:")},
            {name for name in others if not name.startswith("synthetic:")},
        )
        await stored.load_links(stored.rows)
        _link_written(spec, linkable, stored, page, resolution, ids)
    return resolution


def _expect(resolution: Resolution, row: StoredRow) -> None:
    """The page relies on ``row`` as it is now: locked and checked before writing."""
    resolution.expected.setdefault(row.raw_id, row.state)
    resolution.expected_links.setdefault(
        row.raw_id, frozenset(holder for holder, _, _ in row.links or ())
    )
    resolution.locked.add(row.google_data_point_id)


def _link_written(
    spec: DataTypeSpec,
    written: list[tuple[_Group, StoredRow | None]],
    stored: _Rows,
    page: _Page,
    resolution: Resolution,
    ids: list[str],
) -> None:
    """A row this page makes counted (a new row, or one marked deleted until now) and the
    counted rows of other fingerprints of its device that show the same reading are one
    record: all but one are superseded at once, so it is counted once. The one that holds
    the others is a row this page writes (the row Google returned now: a later value under
    its name is written onto it, never hidden behind a stale one), of the device's newest
    form. A growing interval is the same only with the same end: one with another end is
    a value no longer returned, marked by the complete fetch (app/sync/deletions.py), and a
    link would keep this fingerprint's row hidden when that end comes back. A link that
    would close a cycle of links is left out (both count; the reconciler lists it)."""
    chains = page.chains
    first_seen = resolution.state.first_seen if resolution.state is not None else {}
    newly: dict[tuple, list[tuple[_Group, StoredRow | None]]] = defaultdict(list)
    for group, row in written:
        shown = (group.reading, group.identity.shape)
        newly[(group.key, chains.device(group.source), shown)].append((group, row))
    for (key, device, value), entries in sorted(newly.items(), key=lambda item: item[0][:2]):
        # google id -> (fingerprint, end, stored row or None, the page's group or None)
        members: dict[str, tuple[str, str | None, StoredRow | None, _Group | None]] = {}
        for group, row in entries:
            members[ids[group.indexes[0]]] = (group.source, group.identity.shape, row, group)
        for other in stored.of_key.get(key, ()):
            if (
                other.google_data_point_id in members
                or other.identity is None
                or chains.device(other.identity.source) != device
                or not page.counted(other)
                or page.shown(other) != value
            ):
                continue
            writer = page.claimed.get(other.raw_id)
            shape = writer.identity.shape if writer is not None else other.identity.shape
            members[other.google_data_point_id] = (other.identity.source, shape, other, writer)
        if len(members) < 2:
            continue

        def rank(item) -> tuple:
            google_id, (source, _, _, group) = item
            # A fingerprint first seen in this page is the newest form of all.
            first = first_seen.get(source)
            return (group is not None, first.isoformat() if first else "~", google_id)

        holder_id, (holder_source, _, holder_row, holder_group) = max(members.items(), key=rank)
        if holder_group is not None:
            digest, record = holder_group.digest, holder_group.record
        else:
            assert holder_row is not None
            digest, record = holder_row.payload_hash, stored.payload(holder_row)
        beyond = {row.raw_id for row in stored.beyond(holder_row)} if holder_row else set()
        for google_id, (source, _, row, group) in sorted(members.items()):
            if google_id == holder_id or source == holder_source:
                continue
            if row is not None and row.raw_id in beyond:
                continue  # would close a cycle of links
            if group is not None:
                payload = group.record
            else:
                assert row is not None
                payload = stored.payload(row)
            assert isinstance(payload, dict) and isinstance(record, dict)
            resolution.links.append(
                PlannedLink(
                    google_id,
                    holder_id,
                    supersession_reason(spec, payload, record),
                    source,
                    holder_source,
                    digest,
                )
            )
            if row is not None:
                _expect(resolution, row)
            resolution.chained += len(group.indexes) if group is not None else 1
        if holder_row is not None:
            _expect(resolution, holder_row)


def _new_ids(
    fresh: list[_Group],
    by_id: dict[str, uuid.UUID],
    ids: list[str],
    resolution: Resolution,
) -> None:
    """A new row's id must not be one a stored row already has (that row is hidden behind
    another value now): such a record gets the first free spare id (``resolve_ids`` looked
    up the record's spare ids until a free one)."""
    for group in fresh:
        target = new_record_id(group.identity.specific_key)
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


async def add_links(
    session: AsyncSession,
    data_type: str,
    links: Sequence[PlannedLink],
    raw_ids: dict[str, uuid.UUID],
    now: datetime,
) -> int:
    """Insert the planned links (and the superseded row's ``identity-merged`` event, once per
    row ever); ``raw_ids``: google id -> raw id of the rows written or locked."""
    from app.models.identity import MERGED_EVENT
    from app.models.raw_versions import RawHealthDataEvent
    from app.services.storage import dialect_insert

    values = []
    for link in sorted(
        links, key=lambda link: (link.old_google_data_point_id, link.new_google_data_point_id)
    ):
        old = raw_ids.get(link.old_google_data_point_id)
        new = raw_ids.get(link.new_google_data_point_id)
        if old is None or new is None or old == new:
            continue
        values.append(
            {
                "old_raw_id": old,
                "new_raw_id": new,
                "data_type": data_type,
                "old_google_data_point_id": link.old_google_data_point_id,
                "new_google_data_point_id": link.new_google_data_point_id,
                "reason": link.reason,
                "old_fingerprint": link.old_fingerprint,
                "new_fingerprint": link.new_fingerprint,
                "same_reading": True,
                "new_payload_hash": link.holder_payload_hash,
            }
        )
    if not values:
        return 0
    table = RawHealthDataSupersession.__table__
    inserted: list[uuid.UUID] = []
    for offset in range(0, len(values), 1000):
        statement = (
            dialect_insert(session, table)
            .values(values[offset : offset + 1000])
            .on_conflict_do_nothing(index_elements=["old_raw_id", "new_raw_id"])
            .returning(table.c.old_raw_id)
        )
        inserted.extend((await session.execute(statement)).scalars().all())
    names = {value["old_raw_id"]: value["old_google_data_point_id"] for value in values}
    # One identity-merged event per row, ever (a row linked before has one).
    merged = set(
        (
            await session.scalars(
                select(RawHealthDataEvent.raw_id).where(
                    RawHealthDataEvent.event == MERGED_EVENT,
                    among(session, RawHealthDataEvent.raw_id, sorted(set(inserted), key=str)),
                )
            )
        ).all()
    )
    events = [
        {
            "raw_id": raw_id,
            "data_type": data_type,
            "google_data_point_id": names[raw_id],
            "event": MERGED_EVENT,
            "occurred_at": now,
            "previous_deleted_at": None,
        }
        for raw_id in sorted(set(inserted) - merged, key=str)
    ]
    for offset in range(0, len(events), 1000):
        await session.execute(insert(RawHealthDataEvent), events[offset : offset + 1000])
    return len(inserted)


@dataclass
class Presence:
    # Candidates whose record (same reading) a row of their own device was seen holding.
    present: set[uuid.UUID] = field(default_factory=set)
    # Candidates whose record (same reading) a fingerprint that may be their device was seen
    # holding (a possible rename nobody decided, or reprocessed records under a new name):
    # not marked now.
    deferred: set[uuid.UUID] = field(default_factory=set)
    # (candidate's fingerprint, the other fingerprint) -> candidates deferred because of it.
    deferred_pairs: Counter = field(default_factory=Counter)
    # Deferred candidate -> (its fingerprint, the other fingerprint).
    deferred_by: dict[uuid.UUID, tuple[str, str]] = field(default_factory=dict)


async def present_records(
    session: AsyncSession,
    spec: DataTypeSpec,
    candidates: Sequence[tuple[uuid.UUID, object, datetime | None]],
    seen_since: datetime,
    chains: Chains | None = None,
) -> Presence:
    """Candidates (raw id, raw_json, stored start) that must not be marked deleted.

    For each candidate, the rows with its v2 key seen at or after ``seen_since`` decide:

    * one *with the same reading* of its *device* (the same fingerprint, or one joined to it
      by live chain links; every link in force is one of these) - a duplicate of the record
      under another id: present;
    * else one with the same reading of a fingerprint that is not *certainly* another device
      - decided distinct by the owner (``Chains.owner_decided_distinct``: a form of each,
      through links the owner reviewed), or established distinct by evidence between these
      two fingerprints themselves while their keys do not mostly agree and their sightings
      do not merely touch (``Chains.certainly_distinct``) - or of one established so whose
      keys in this chunk mostly came back with the candidates' readings (records reprocessed
      under a new name):
      deferred, until the reconciler or the owner has decided - also when the candidate's
      own device returned another value, since the chain that makes it one device may be
      the wrong one;
    * else missing: not returned, or its device returned another value. Of two devices that
      recorded at one instant, one can be deleted alone.

    Nothing is written here (evidence of two devices comes only from responses, in the
    write path). ``chains``: the data type's chains with every span (loaded when not given).
    """
    from app.core.timeutil import as_utc
    from app.parsers.common import temporal

    presence = Presence()
    # Each candidate's instants: its parsed start and (a stale one, written by an older
    # parser, still finds the other rows) its stored start. Only candidates at an instant some
    # row was seen at are identified (hashed) at all.
    instants: dict[uuid.UUID, set[datetime]] = {}
    for raw_id, raw_json, start in candidates:
        found = {as_utc(start)} if start is not None else set()
        parsed = (
            temporal(spec, raw_json)[0]
            if isinstance(raw_json, dict) and google_name(raw_json) is None
            else None
        )
        if parsed is not None:
            found.add(as_utc(parsed))
        instants[raw_id] = found
    starts = set().union(*instants.values()) if instants else set()
    if not starts:
        return presence
    seen_rows: dict[str, list[tuple[uuid.UUID, str, str | None, str | None]]] = defaultdict(list)
    seen_at: set[datetime] = set()
    for chunk in _chunks(sorted(starts)):
        rows = await session.execute(
            select(RawHealthData.id, RawHealthData.raw_json, RawHealthData.source_start_time).where(
                RawHealthData.data_type == spec.name,
                among(session, RawHealthData.source_start_time, chunk),
                RawHealthData.last_seen_at >= seen_since,
            )
        )
        for raw_id, raw_json, start in rows.tuples():
            if start is not None:
                seen_at.add(start)
            identity = record_identity(spec, raw_json)
            if identity is not None and identity.start is not None:
                seen_at.add(identity.start)
            if identity is not None:
                seen_rows[identity.key].append(
                    (raw_id, identity.source, reading(spec, raw_json), identity.shape)
                )
    if not seen_rows:
        return presence
    seen_starts = {as_utc(start) for start in seen_at}
    relevant = [
        (raw_id, raw_json) for raw_id, raw_json, _ in candidates if instants[raw_id] & seen_starts
    ]
    identities = {raw_id: record_identity(spec, raw_json) for raw_id, raw_json in relevant}
    values = {raw_id: reading(spec, raw_json) for raw_id, raw_json in relevant}
    if chains is None:
        chains = await load_chains(session, spec.name)
    # Per (candidate fingerprint, other fingerprint): keys the other returned with the
    # candidate's reading, and with another one.
    tally: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for raw_id, identity in identities.items():
        if identity is None:
            continue
        returned: dict[str, bool] = {}
        for other_id, source, value, _ in seen_rows.get(identity.key, []):
            if other_id != raw_id and not chains.same_device(identity.source, source):
                returned[source] = returned.get(source, False) or value == values[raw_id]
        for source, same in returned.items():
            tally[(identity.source, source)][0 if same else 1] += 1
    for raw_id, identity in identities.items():
        if identity is None:
            continue
        same_value = [
            (source, shape)
            for other_id, source, value, shape in seen_rows.get(identity.key, [])
            if other_id != raw_id and value == values[raw_id]
        ]
        # Its device returned it as it is: for a growing interval, with this end too (a row
        # of the device with another end returned now shows the record as Google has it; this
        # end is gone, and it would hide that one).
        if any(
            chains.same_device(identity.source, source) and shape == identity.shape
            for source, shape in same_value
        ):
            presence.present.add(raw_id)
            continue
        same_value_sources = [source for source, _ in same_value]
        undecided = sorted(
            {
                source
                for source in same_value_sources
                if not chains.same_device(identity.source, source)
                and not chains.owner_decided_distinct(identity.source, source)
                and (
                    not chains.certainly_distinct(identity.source, source)
                    or tally[(identity.source, source)][0] >= tally[(identity.source, source)][1]
                )
            }
        )
        if undecided:
            presence.deferred.add(raw_id)
            presence.deferred_pairs[(identity.source, undecided[0])] += 1
            presence.deferred_by[raw_id] = (identity.source, undecided[0])
    return presence


def deferred_summary(chains: Chains, pairs: Counter) -> list[dict]:
    """The pairs of sources that deferred deletions, for status (labels, short prints)."""

    def label(name: str) -> str:
        span = chains.spans.get(name)
        return span.label if span is not None else name[:12]

    merged: Counter = Counter()
    for (row_source, other), count in pairs.items():
        merged[(row_source, other)] += count
    return [
        {
            "rows_of": label(row_source),
            "returned_by": label(other),
            "fingerprints": [row_source[:12], other[:12]],
            "deferred": count,
            "decide": "identity-decide distinct (two devices: mark them) or not-distinct and "
            "link (one device renamed)",
            "pair": list(ordered_pair(row_source, other)),
        }
        for (row_source, other), count in sorted(merged.items(), key=lambda item: -item[1])
    ]


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
    errors.extend(await _foreign_history(session))
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
            old_row.last_seen_at,
            new_row.last_seen_at,
            new_row.is_quarantined,
        )
        .join(old_row, old_row.id == link.old_raw_id)
        .join(new_row, new_row.id == link.new_raw_id)
    )
    differing = void = stale = 0
    # Rows superseded in force, and the rows they point at (a holder superseded itself).
    superseded_rows: set[uuid.UUID] = set()
    targets: set[uuid.UUID] = set()
    result = await session.stream(links.execution_options(yield_per=1_000))
    async for row in result:
        raw_id, holder_id, data_type, old_source, new_source = row[:5]
        old_json, new_json, old_deleted, new_deleted, old_seen, new_seen, held_aside = row[5:]
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
        if old_deleted is None and new_deleted is None:
            if reading(spec, old_json) != reading(spec, new_json):
                differing += 1
            if (
                not held_aside
                and as_utc(old_seen) > as_utc(new_seen)
                and (reading(spec, old_json), old.shape) != (reading(spec, new_json), new.shape)
            ):
                stale += 1
    if differing:
        notes.append(
            f"{differing} live superseded row(s) hold a value their holder no longer has (an "
            "older value of the record; the next complete fetch of their window marks them)"
        )
    if stale:
        notes.append(
            f"{stale} superseded row(s) were returned after the row hiding them, with another "
            "reading or end: hidden behind a stale holder until a complete fetch of their window "
            "marks it (a fetch that was not complete leaves it)"
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
    for data_type, (doubled, by_one) in sorted((await _counted_twice(session, chains)).items()):
        if doubled:
            notes.append(
                f"{doubled} record(s) of {data_type} are counted more than once within one "
                "device (rows of one key and reading that no link in force joins yet): the next "
                "identity-backfill links them, unless they are listed under conflicts"
            )
        if by_one:
            notes.append(
                f"{by_one} record(s) of {data_type} have more than one counted row of one source "
                "(an older value counted beside the one returned last, which a reading of its "
                "own kept visible): the next complete fetch of their window marks the one no "
                "longer returned"
            )
    errors.extend(await _verify_agreement(session))
    if session.get_bind().dialect.name == "postgresql":
        notes.extend(await _index_notes(session))
    return count, errors, notes


_BUILD_DELETED_INDEX = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_raw_health_data_source_deleted ON "
    "raw_health_data (data_type, google_data_point_id) WHERE source_deleted_at IS NOT NULL"
)


async def _index_notes(session: AsyncSession) -> list[str]:
    """The partial index of 0012 missing, and every invalid index on raw_health_data (an
    interrupted concurrent build or rebuild, ``..._ccnew``), each with its statement."""
    notes = []
    valid = (await session.execute(_DELETED_INDEX_SQL)).scalar_one_or_none()
    if valid is None:
        notes.append(
            "index ix_raw_health_data_source_deleted is missing (the analytics views are slower "
            f"without it): build it with {_BUILD_DELETED_INDEX} when no dump or long "
            "transaction runs (docs/RUNBOOK.md)"
        )
    for (name,) in (await session.execute(_INVALID_INDEXES_SQL)).tuples():
        if name == "ix_raw_health_data_source_deleted":
            notes.append(
                f"index {name} is invalid (an interrupted concurrent build; it is not used): "
                f"rebuild it with REINDEX INDEX CONCURRENTLY {name} when no dump or long "
                "transaction runs (docs/RUNBOOK.md)"
            )
        else:
            notes.append(
                f"index {name} on raw_health_data is invalid (left by an interrupted concurrent "
                "build or REINDEX; it holds no data of its own and is not used, but writers "
                f"keep it up to date): DROP INDEX CONCURRENTLY {name} removes it, or REINDEX "
                f"INDEX CONCURRENTLY {name} rebuilds it (docs/RUNBOOK.md)"
            )
    return notes


async def _verify_agreement(session: AsyncSession) -> list[str]:
    """The agreement counts per pair (identity_reconcile_state.pair_agreement, which decide
    whether a pair is taken for two devices) are the latest rows of raw_source_agreements
    summed."""
    from app.models.identity import IdentityReconcileState, RawSourceAgreement
    from app.services.source_chains import parse_agreement

    table = RawSourceAgreement
    latest: dict[tuple[str, str, str, str], bool] = {}
    result = await session.stream(
        select(
            table.data_type,
            table.first_fingerprint,
            table.second_fingerprint,
            table.identity_key,
            table.same_reading,
        )
        .order_by(table.id)
        .execution_options(yield_per=10_000)
    )
    async for data_type, first, second, key, same in result:
        latest[(data_type, first, second, key)] = same
    summed: dict[str, dict[tuple[str, str], list[int]]] = defaultdict(dict)
    for (data_type, first, second, _), same in latest.items():
        counts = summed[data_type].setdefault((first, second), [0, 0])
        counts[0 if same else 1] += 1
    stored = {
        state.data_type: parse_agreement(state.pair_agreement)
        for state in (await session.scalars(select(IdentityReconcileState))).all()
    }
    errors = []
    for data_type in sorted(set(summed) | set(stored)):
        expected = {pair: tuple(counts) for pair, counts in summed.get(data_type, {}).items()}
        actual = {
            pair: (agree, disagree)
            for pair, (agree, disagree, _) in stored.get(data_type, {}).items()
            if agree or disagree
        }
        if expected != actual:
            errors.append(
                f"the agreement counts of {data_type} (identity_reconcile_state.pair_agreement) "
                "are not the sum of raw_source_agreements: they decide whether two sources are "
                "taken for two devices; run identity-backfill (a full run) to count them again"
            )
    return errors


async def _foreign_history(session: AsyncSession) -> list[str]:
    """A row without a Google id holds the payloads of one source only: a payload written onto
    another source's row would hide one source's history in another's."""
    from app.models.raw_versions import RawHealthDataVersion
    from app.services.identity_backfill import spec_for

    found: dict[str, list[uuid.UUID]] = defaultdict(list)
    if session.get_bind().dialect.name == "postgresql":
        for data_type, raw_id in (await session.execute(_FOREIGN_HISTORY_SQL)).tuples():
            found[data_type].append(raw_id)
    else:
        result = await session.stream(
            select(
                RawHealthData.data_type,
                RawHealthData.id,
                RawHealthData.raw_json,
                RawHealthDataVersion.raw_json,
            ).join(RawHealthDataVersion, RawHealthDataVersion.raw_id == RawHealthData.id)
        )
        async for data_type, raw_id, current, earlier in result:
            if google_name(current) or google_name(earlier):
                continue
            spec = spec_for(data_type)
            now, then = record_identity(spec, current), record_identity(spec, earlier)
            if now is not None and then is not None and now.source != then.source:
                found[data_type].append(raw_id)
    return [
        f"{len(set(raw_ids))} raw row(s) of {data_type} hold payloads of more than one source "
        f"in their history (e.g. {sorted(set(raw_ids), key=str)[0]}): one source's payload "
        "was written onto another source's row"
        for data_type, raw_ids in sorted(found.items())
    ]


async def _counted_twice(
    session: AsyncSession, chains: dict[str, Chains]
) -> dict[str, tuple[int, int]]:
    """Per data type, the records counted more than once within one device - rows of one key,
    one device (as the live chains make it) and one reading that readers all count - and
    the records with more than one counted row of one fingerprint (two values of one source
    at one instant: one of them is no longer returned)."""
    from app.models.identity import held_elsewhere
    from app.services.identity_backfill import spec_for

    counted = (
        RawHealthData.source_deleted_at.is_(None),
        RawHealthData.is_quarantined.is_(False),
        RawHealthData.source_start_time.is_not(None),
        ~held_elsewhere(RawHealthData.id),
    )
    shared = (
        select(RawHealthData.data_type, RawHealthData.source_start_time)
        .where(*counted)
        .group_by(RawHealthData.data_type, RawHealthData.source_start_time)
        .having(func.count() > 1)
        .subquery()
    )
    query = (
        select(RawHealthData.data_type, RawHealthData.source_start_time, RawHealthData.raw_json)
        .join(
            shared,
            (shared.c.data_type == RawHealthData.data_type)
            & (shared.c.source_start_time == RawHealthData.source_start_time),
        )
        .where(*counted)
        .order_by(RawHealthData.data_type, RawHealthData.source_start_time)
    )
    doubled: Counter = Counter()
    twice_by_one: Counter = Counter()
    group: Counter = Counter()
    by_source: Counter = Counter()
    current: tuple | None = None

    def close() -> None:
        if current is not None:
            doubled[current[0]] += sum(1 for count in group.values() if count > 1)
            twice_by_one[current[0]] += sum(1 for count in by_source.values() if count > 1)

    result = await session.stream(query.execution_options(yield_per=5_000))
    async for data_type, start, raw_json in result:
        if (data_type, start) != current:
            close()
            current, group, by_source = (data_type, start), Counter(), Counter()
        spec = spec_for(data_type)
        identity = record_identity(spec, raw_json)
        if identity is None:
            continue
        device = (chains.get(data_type) or Chains()).device(identity.source)
        group[(identity.key, device, reading(spec, raw_json))] += 1
        by_source[(identity.key, identity.source)] += 1
    close()
    return {
        data_type: (doubled[data_type], twice_by_one[data_type])
        for data_type in set(doubled) | set(twice_by_one)
        if doubled[data_type] or twice_by_one[data_type]
    }


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
    "raw_source_agreements": {
        "pk_raw_source_agreements": "PRIMARY KEY (id)",
        "ck_raw_source_agreements_ordered": (
            "CHECK (((first_fingerprint)::text < (second_fingerprint)::text))"
        ),
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
    "WHERE c.relkind = 'v' AND (c.relnamespace = pg_my_temp_schema() OR "
    "(n.nspname = current_schema() AND c.relname = ANY(:views)))"
)
# Rows whose earlier payloads came from another source than the current one (PostgreSQL;
# records with their own Google id may change source, every other row never does).
_FOREIGN_HISTORY_SQL = text(
    "SELECT DISTINCT r.data_type, r.id FROM raw_health_data_versions v "
    "JOIN raw_health_data r ON r.id = v.raw_id "
    "WHERE NOT (r.raw_json ? 'name' OR r.raw_json ? 'dataPointName' "
    "OR v.raw_json ? 'name' OR v.raw_json ? 'dataPointName') "
    # A missing dataSource and a JSON null one are the same source (one fingerprint).
    "AND COALESCE(v.raw_json -> 'dataSource', 'null'::jsonb) "
    "IS DISTINCT FROM COALESCE(r.raw_json -> 'dataSource', 'null'::jsonb)"
)
_INVALID_INDEXES_SQL = text(
    "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = current_schema() AND i.indrelid = 'raw_health_data'::regclass "
    "AND NOT i.indisvalid ORDER BY c.relname"
)
_DELETED_INDEX_SQL = text(
    "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = current_schema() AND c.relname = 'ix_raw_health_data_source_deleted'"
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
