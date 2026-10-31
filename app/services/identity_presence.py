"""Evaluate unseen deletion candidates against the records returned by Google."""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.google_health.data_types import DataTypeSpec
from app.models.identity import RawHealthDataSupersession
from app.models.state import RawHealthData
from app.parsers.common import google_name
from app.parsers.identity import RecordIdentity, reading, record_identity
from app.services.identity_common import _chunks, among
from app.services.source_chains import Chains, load_chains, ordered_pair


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

    For each candidate, the rows with its match key and a kindred source (its v2 key, or
    the key of a form of its source with an optional device field added or dropped) seen at or
    after ``seen_since`` decide:

    * one *with the same reading* of its *device* (the same fingerprint, or one joined to it
      by live chain links; every link in force is one of these) - a duplicate of the record
      under another id - and, for a growing interval, either a row it is hidden behind (a link
      in force) with the same end or a later one (the period grew, under either form, however
      the two were linked meanwhile), or a row of another fingerprint with the same end:
      present;
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
    seen_rows: dict[str, list[tuple[uuid.UUID, str, str | None, str | None, RecordIdentity]]] = (
        defaultdict(list)
    )
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
                seen_rows[identity.match].append(
                    (
                        raw_id,
                        identity.source,
                        reading(spec, raw_json),
                        identity.shape,
                        identity,
                    )
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
    # Growing intervals: the rows each candidate is hidden behind (links in force to a row
    # neither marked nor quarantined).
    holders = await _holders(
        session,
        [raw_id for raw_id, identity in identities.items() if identity and identity.shape],
    )
    # Per (candidate fingerprint, other fingerprint): keys the other returned with the
    # candidate's reading, and with another one.
    tally: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])

    def seen_of(identity: RecordIdentity) -> list[tuple[uuid.UUID, str, str | None, str | None]]:
        # The rows seen of its record under a form of its device, or a source kindred to its
        # own or to another form of its device (never one with another value of a device field).
        return [
            (other_id, source, value, shape)
            for other_id, source, value, shape, other in seen_rows.get(identity.match, [])
            if identity.compatible(other)
            and (
                chains.same_device(identity.source, source)
                or identity.kindred(other)
                or any(
                    chains.kindred(member, source)
                    for member in chains.members(identity.source)
                    if member in chains.spans and source in chains.spans
                )
            )
        ]

    for raw_id, identity in identities.items():
        if identity is None:
            continue
        returned: dict[str, bool] = {}
        for other_id, source, value, _ in seen_of(identity):
            if other_id != raw_id and not chains.same_device(identity.source, source):
                returned[source] = returned.get(source, False) or value == values[raw_id]
        for source, same in returned.items():
            tally[(identity.source, source)][0 if same else 1] += 1
    for raw_id, identity in identities.items():
        if identity is None:
            continue
        same_value = [
            (other_id, source, shape)
            for other_id, source, value, shape in seen_of(identity)
            if other_id != raw_id and value == values[raw_id]
        ]
        if any(
            chains.same_device(identity.source, source)
            and (
                shape == identity.shape
                if identity.shape is None
                else _keeps(identity, source, shape, other_id in holders.get(raw_id, ()))
            )
            for other_id, source, shape in same_value
        ):
            presence.present.add(raw_id)
            continue
        same_value_sources = [source for _, source, _ in same_value]
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


async def _holders(
    session: AsyncSession, raw_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """Raw id -> the rows it is hidden behind: its links in force to a row that is neither
    marked deleted nor quarantined (``held_elsewhere``)."""
    from app.models.identity import in_force

    found: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    link, holder = aliased(RawHealthDataSupersession), aliased(RawHealthData)
    for chunk in _chunks(sorted(set(raw_ids), key=str)):
        rows = await session.execute(
            select(link.old_raw_id, link.new_raw_id)
            .join(holder, holder.id == link.new_raw_id)
            .where(
                among(session, link.old_raw_id, chunk),
                holder.source_deleted_at.is_(None),
                holder.is_quarantined.is_(False),
                in_force(link),
            )
        )
        for old, new in rows.tuples():
            found[old].add(new)
    return found


def _keeps(identity: RecordIdentity, source: str, shape: str | None, holds: bool) -> bool:
    """A growing interval returned by its device (``source``, end ``shape``) keeps the unseen
    row of ``identity``: the row stays hidden behind the returned row (``holds``: a link in
    force to it) and the period grew or kept its end, under either form - however the two were
    linked meanwhile; or another fingerprint of its device returned this very end. Otherwise
    the row would count beside the end returned now: an end no longer returned (a longer one,
    or a row whose holder is not returned), or a copy its own fingerprint holds in another row
    (written while this row was hidden behind another end)."""
    if holds:
        return (
            shape is not None
            and identity.shape is not None
            and (datetime.fromisoformat(shape) >= datetime.fromisoformat(identity.shape))
        )
    return source != identity.source and shape == identity.shape


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
