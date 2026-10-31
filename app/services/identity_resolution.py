"""Stored row loading and page-local decisions for v2 record resolution."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy import String, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.google_health.data_types import DataTypeSpec
from app.models.identity import RawHealthDataIdentity, RawHealthDataSupersession
from app.models.state import RawHealthData
from app.parsers.identity import (
    RecordIdentity,
    compatible,
    new_record_id,
    reading,
    record_identity,
    spare_record_id,
    supersession_reason,
)
from app.services.identity_common import (
    _CHUNK,
    MAX_CHAIN,
    PlannedLink,
    Resolution,
    StoredRow,
    _chunks,
    _Group,
    among,
)
from app.services.source_chains import Chains

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
        # v2 key -> its rows (every source); match key -> its rows (every source).
        self.of_key: dict[str, list[StoredRow]] = defaultdict(list)
        self.of_match: dict[str, list[StoredRow]] = defaultdict(list)
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
            self.of_match[row.identity.match].append(row)
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
                    self.of_match[row.identity.match].append(row)

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


async def _data_sources(
    session: AsyncSession, data_type: str, fingerprints: set[str]
) -> dict[str, object]:
    """The ``dataSource`` of each of these fingerprints that has a sighting."""
    from app.models.identity import RawSourceSighting

    found: dict[str, object] = {}
    for chunk in _chunks(sorted(fingerprints)):
        rows = await session.execute(
            select(RawSourceSighting.fingerprint, RawSourceSighting.data_source).where(
                RawSourceSighting.data_type == data_type,
                among(session, RawSourceSighting.fingerprint, chunk),
            )
        )
        found.update(rows.tuples().all())
    return found


def _expect(resolution: Resolution, row: StoredRow) -> None:
    """The page relies on ``row`` as it is now: locked and checked before writing."""
    resolution.expected.setdefault(row.raw_id, row.state)
    if row.links is not None:  # (links beyond MAX_CHAIN steps were not read)
        resolution.expected_links.setdefault(
            row.raw_id, frozenset(holder for holder, _, _ in row.links)
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
        newly[(group.identity.match, chains.device(group.source), shown)].append((group, row))
    for (key, device, value), entries in sorted(newly.items(), key=lambda item: item[0][:2]):
        # google id -> (fingerprint, end, stored row or None, the page's group or None)
        members: dict[str, tuple[str, str | None, StoredRow | None, _Group | None]] = {}
        for group, row in entries:
            members[ids[group.indexes[0]]] = (group.source, group.identity.shape, row, group)
        for other in stored.of_match.get(key, ()):
            if (
                other.google_data_point_id in members
                or other.identity is None
                or not other.identity.compatible(entries[0][0].identity)
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
            if not compatible(payload.get("dataSource"), record.get("dataSource")):
                # Two values of a device field are never one record, whatever the chains say.
                continue
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
