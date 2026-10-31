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

1. a row of its own fingerprint that leaves its reading counted (``_best``: not hidden, or its
   hiders show the same reading - a growing interval also the same end), the counted one
   first; spare rows of its own are reused like any other;
2. else a new row of its own (``synthetic:v2s:<hash of key and fingerprint>``, or the first
   free *spare* id of the record and fingerprint when that id is taken): a reading Google
   returns is never hidden behind a row showing another value.

A row the page makes counted (new, or marked deleted until now) holds at once every counted row
of another fingerprint of its device (joined by live chain links) that shows the same reading
(``_link_written``), so a decided rename is never counted twice and the row Google returned now
holds the record; with no such row it counts on its own (an unknown fingerprint, a second
device, a revised value), never hidden. Stored rows are found by id only: the record's v1 id,
its ``v2s`` id, its spare ids (in blocks, when none of its rows fits) and its alias; the same for
the other fingerprints of its device. ``store_records`` locks the rows it relies on (the rows it
writes onto, every row that could hide them, the rows it links) and re-resolves when one
changed, was revived or linked meanwhile.

Rows of two fingerprints are compared by their *match key* (app/parsers/identity.py: the v2 key
without the optional device fields), when the two are kindred (app/parsers/identity.py): Google
adding ``formFactor`` to a band's records changes their key, not the record.

``present_records`` decides, for deletion marking, whether an unseen row's record is still
returned: with the same reading under its own device (present); with the same reading under a
fingerprint not *certainly* another device (deferred: possibly a rename nobody decided yet, or
reprocessed records returned under a new name); or not at all, or only with another value
(missing).

Page orchestration stays here so callers can replace ``identity.lock_data_type``; the
supporting decisions, writes, presence checks and integrity checks have separate modules.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.google_health.data_types import DataTypeSpec
from app.parsers.common import source_id
from app.parsers.identity import RecordIdentity as RecordIdentity
from app.parsers.identity import (
    key_under,
    new_record_id,
    pair_key,
    reading,
    record_identity,
    source_label,
    spare_record_id,
    specific_key,
    stable_class,
)
from app.services.identity_common import MAX_CHAIN as MAX_CHAIN
from app.services.identity_common import SPARE_BLOCK as SPARE_BLOCK
from app.services.identity_common import PlannedLink as PlannedLink
from app.services.identity_common import Resolution as Resolution
from app.services.identity_common import StoredRow as StoredRow
from app.services.identity_common import _Group
from app.services.identity_common import aliases as aliases
from app.services.identity_common import among as among
from app.services.identity_common import rows_by_key as rows_by_key
from app.services.identity_presence import Presence as Presence
from app.services.identity_presence import deferred_summary as deferred_summary
from app.services.identity_presence import present_records as present_records
from app.services.identity_resolution import (
    _best,
    _data_sources,
    _expect,
    _link_written,
    _new_ids,
    _Page,
    _Rows,
)
from app.services.identity_schema import _VIEWS_SQL as _VIEWS_SQL
from app.services.identity_schema import EXPECTED_CONSTRAINTS as EXPECTED_CONSTRAINTS
from app.services.identity_schema import identity_tables_missing as identity_tables_missing
from app.services.identity_schema import verify_identity_schema as verify_identity_schema
from app.services.identity_schema import verify_views as verify_views
from app.services.identity_verification import verify_identities as verify_identities
from app.services.identity_writes import add_aliases as add_aliases
from app.services.identity_writes import add_links as add_links
from app.services.source_chains import Chains as Chains
from app.services.source_chains import SourceState as SourceState
from app.services.source_chains import load_state, lock_data_type


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
    by_match: dict[str, list[_Group]] = defaultdict(list)
    for group in groups.values():
        by_match[group.identity.match].append(group)
    # One response returned this record under two kindred fingerprints with two values:
    # evidence of two devices (one record has one value).
    resolution.evidence = [
        (left.source, right.source, pair_key(left.identity, right.identity))
        for found in by_match.values()
        for position, left in enumerate(found)
        for right in found[position + 1 :]
        if left.reading != right.reading and left.identity.kindred(right.identity)
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
        forms = await _data_sources(
            session,
            spec.name,
            {source for group, _ in linkable for source in chains.members(group.source)},
        )
        for group, _ in linkable:
            for source in chains.members(group.source) - {group.source}:
                # Its key for this record: another one when their optional device fields differ.
                key = group.key
                if source in forms and stable_class(forms[source]) != group.identity.stable:
                    if not isinstance(group.record, dict):
                        raise TypeError(
                            f"a record of {spec.name} with a v2 identity is not an object"
                        )
                    key = key_under(spec, group.record, forms[source]) or key
                other = specific_key(key, source)
                others |= {new_record_id(other), other}
        await stored.find(
            {name for name in others if name.startswith("synthetic:")},
            {name for name in others if not name.startswith("synthetic:")},
        )
        await stored.load_links(stored.rows)
        _link_written(spec, linkable, stored, page, resolution, ids)
    return resolution
