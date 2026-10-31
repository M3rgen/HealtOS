"""Per-record alias and supersession planning for identity backfills."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.google_health.data_types import DataTypeSpec
from app.models.identity import RawHealthDataSupersession
from app.parsers.identity import new_record_id, reading, supersession_reason
from app.services.identity import aliases, among
from app.services.identity_backfill_agreement import _tally_agreement
from app.services.identity_backfill_reading import _EPOCH, _Row
from app.services.identity_backfill_state import _chunked
from app.services.identity_backfill_stats import BackfillStats
from app.services.source_chains import Chains


@dataclass
class _Plan:
    aliases: dict[str, _Row] = field(default_factory=dict)
    links: list[tuple[_Row, _Row, str]] = field(default_factory=list)


@dataclass(frozen=True)
class _Link:
    old: uuid.UUID
    new: uuid.UUID
    old_source: str
    new_source: str
    created: datetime


async def _links_of(session: AsyncSession, ids: Sequence[uuid.UUID]) -> list[_Link]:
    """Every supersession whose old or new row is one of ``ids`` (any state)."""
    link = RawHealthDataSupersession
    found: dict[tuple[uuid.UUID, uuid.UUID], _Link] = {}
    for chunk in _chunked(sorted(ids), 5000):
        for column in (link.old_raw_id, link.new_raw_id):
            rows = await session.execute(
                select(
                    link.old_raw_id,
                    link.new_raw_id,
                    link.old_fingerprint,
                    link.new_fingerprint,
                    link.created_at,
                ).where(among(session, column, chunk))
            )
            for old, new, old_source, new_source, created in rows.tuples():
                found[(old, new)] = _Link(old, new, old_source, new_source, created)
    return list(found.values())


async def _plan_batch(
    session: AsyncSession,
    spec: DataTypeSpec,
    rows: list[_Row],
    chains: Chains,
    stats: BackfillStats,
    *,
    count_groups: bool = True,
) -> _Plan:
    """Per key and device, in two steps:

    1. per fingerprint, its *root* (live first, then the one seen last): every other row of
       that fingerprint not yet superseded by one of its own is linked to it (a duplicate, an
       older shape or value of what the source returns; such a link is always in force);
    2. per device, its *holder* among the roots that are counted (live first, the one the
       others already link to, the one that already holds others, then the newest): every
       other counted root that measured the same is linked to it (a value of its own is
       never hidden: ``kept_differing``).

    So only roots get links across fingerprints: when a chain is rejected or refuted, each
    fingerprint still counts once, and two forms joined later are linked root to root. The
    rows a newly linked row held are linked to its holder too (one step for readers), where
    they are of the holder's fingerprint or measured the same. A link that would close a
    cycle of links (in any state) is not made (``conflicts``). Links no longer in force are
    history: their rows are settled again like any other."""
    plan = _Plan()
    groups: dict[str, list[_Row]] = defaultdict(list)
    for row in rows:
        groups[row.identity.match].append(row)
    alias = await aliases(session, spec.name, {row.identity.specific_key for row in rows})
    links = await _links_of(session, [row.raw_id for row in rows])
    by_id = {row.raw_id: row for row in rows}
    existing = {(link.old, link.new) for link in links}
    edges: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    # Rows superseded by a row of their own fingerprint (always in force), rows superseded
    # in force by a counted row (hidden from readers), and the rows each row holds in force.
    own_superseded: set[uuid.UUID] = set()
    hidden: set[uuid.UUID] = set()
    dependents: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for link in links:
        edges[link.old].add(link.new)
        if link.old_source == link.new_source:
            own_superseded.add(link.old)
        if chains.in_force(link.old_source, link.new_source):
            dependents[link.new].append(link.old)
            holder = by_id.get(link.new)
            # Hidden like readers see it (held_elsewhere): its holder is neither marked
            # deleted nor quarantined.
            if holder is None or not (holder.deleted or holder.quarantined):
                hidden.add(link.old)
    pointed = set(dependents)

    def reaches(start: uuid.UUID, target: uuid.UUID) -> bool:
        seen, frontier = {start}, [start]
        while frontier:
            for following in edges.get(frontier.pop(), ()):
                if following == target:
                    return True
                if following not in seen:
                    seen.add(following)
                    frontier.append(following)
        return False

    def measured(row: _Row) -> str | None:
        return reading(spec, row.raw_json)

    def newest_form(row: _Row) -> str:
        span = chains.spans.get(row.identity.source)
        return span.first.isoformat() if span is not None else ""

    def add_link(row: _Row, holder: _Row) -> None:
        plan.links.append((row, holder, supersession_reason(spec, row.raw_json, holder.raw_json)))
        edges[row.raw_id].add(holder.raw_id)
        existing.add((row.raw_id, holder.raw_id))
        dependents[holder.raw_id].append(row.raw_id)
        hidden.add(row.raw_id)
        # What the row held, the holder holds now (one step for readers): rows of the
        # holder's fingerprint, and rows of others that measured the same.
        for dependent in list(dependents.get(row.raw_id, ())):
            held = by_id.get(dependent)
            if (
                held is not None
                and not held.deleted
                and (
                    held.identity.source == holder.identity.source
                    or (
                        measured(held) == measured(holder)
                        and held.identity.compatible(holder.identity)
                    )
                )
                and (dependent, holder.raw_id) not in existing
                and not reaches(holder.raw_id, dependent)
            ):
                plan.links.append(
                    (held, holder, supersession_reason(spec, held.raw_json, holder.raw_json))
                )
                edges[dependent].add(holder.raw_id)
                existing.add((dependent, holder.raw_id))
                dependents[holder.raw_id].append(dependent)

    def join(row: _Row, holder: _Row) -> bool:
        if (row.raw_id, holder.raw_id) in existing:
            return True
        if not row.identity.compatible(holder.identity):
            # Never one record (two values of a device field): no link ever joins them, whatever
            # the chains say (listed with the conflicts; both count).
            stats.conflicts += not row.deleted
            return False
        if reaches(holder.raw_id, row.raw_id):
            # Left double (listed) unless the row is marked deleted: then it counts nowhere.
            stats.conflicts += not row.deleted
            return False
        add_link(row, holder)
        return True

    for members in groups.values():
        if count_groups:
            stats.identities += 1
        devices: dict[str, list[_Row]] = defaultdict(list)
        for row in members:
            devices[chains.device(row.identity.source)].append(row)
        if count_groups:
            if len(devices) > 1:
                stats.distinct_sources += 1
        _tally_agreement(spec, members, chains, stats)
        for device in devices.values():
            if count_groups:
                stats.already_superseded += sum(row.raw_id in hidden for row in device)
            by_source: dict[str, list[_Row]] = defaultdict(list)
            for row in device:
                by_source[row.identity.source].append(row)
            roots: list[_Row] = []
            for own in by_source.values():
                candidates = [row for row in own if row.raw_id not in own_superseded] or own
                root = max(
                    candidates,
                    key=lambda row: (
                        not row.deleted,
                        # A growing interval: the end returned last holds the record.
                        row.last_seen_at if row.identity.shape else _EPOCH,
                        row.identity.shape or "",
                        row.last_seen_at,
                        row.raw_id in pointed,
                        row.first_seen_at,
                        str(row.raw_id),
                    ),
                )
                for row in candidates:
                    if row is not root:
                        join(row, root)
                roots.append(root)
            # A root marked deleted counts nowhere, and a link would keep it hidden when its
            # record comes back with another value (a row of its own again, every time):
            # only live roots are linked across fingerprints.
            counted = [root for root in roots if root.raw_id not in hidden and not root.deleted]
            if len(counted) > 1:
                holder = max(
                    counted,
                    key=lambda row: (
                        # A growing interval: the end returned last holds the record (an end
                        # Google no longer returns is stale, longer or not).
                        row.last_seen_at if row.identity.shape else _EPOCH,
                        row.identity.shape or "",
                        sum((other.raw_id, row.raw_id) in existing for other in counted),
                        row.raw_id in pointed,
                        row.last_seen_at,
                        # Of rows returned together, the device's newest form (the one
                        # returned again, as a rule).
                        newest_form(row),
                        row.first_seen_at,
                        str(row.raw_id),
                    ),
                )
                value = measured(holder)
                for root in counted:
                    if root is holder:
                        continue
                    if measured(root) != value:
                        # Another fingerprint that measured something else: never hidden.
                        stats.kept_differing += 1
                        continue
                    join(root, holder)
            if count_groups and all(row.deleted for row in device):
                holder = max(device, key=lambda row: row.order)
                others = [
                    row
                    for row in members
                    if row not in device
                    and not row.deleted
                    and any(row.identity.kindred(own.identity) for own in device)
                    and not chains.distinct(row.identity.source, holder.identity.source)
                ]
                if others:
                    stats.deletions_suspicious += 1
                else:
                    stats.deletions_kept += 1
        # Every row reachable by its key and fingerprint: its fingerprint's root first.
        for row in sorted(
            members,
            key=lambda row: (row.raw_id not in own_superseded, row.order),
            reverse=True,
        ):
            name = row.identity.specific_key
            if row.google_data_point_id == new_record_id(name):
                continue
            if name in alias:
                if count_groups:
                    stats.aliases_present += 1
                continue
            plan.aliases.setdefault(name, row)
    return plan
