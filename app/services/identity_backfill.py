"""``health-mirror identity-backfill``: source chains, v2 aliases and supersession links.

Rows stored before migration 20261021_0012 have no alias, and rows archived twice under
older ids of the same record (a device rename, a package suffix change, civil times added
to an interval, a growing interval) are separate live rows. Per data type the command runs
two passes over every row without a Google id (``raw_health_data`` is only read):

1. **Timeline.** Every row and every earlier version gives a sighting of its source
   fingerprint (first received .. last seen) and the v2 keys it shares with other
   fingerprints. The spans are merged into ``raw_source_sightings`` (complete from now on),
   and the chains are decided per source by the rule of app/services/source_chains.py: a
   fingerprint follows another of its stable class only when they share keys, the old one
   was last seen more than five minutes before the new one first appeared, and it is the
   only such candidate. New links go to ``raw_source_chains`` (append-only).
2. **Links.** Per v2 key, the rows are grouped by device (same fingerprint, or one chain).
   Within one device the row that holds the record (one already linked to, else the
   aliased one, else the newest live row; a deleted-marked row only when the device has no
   live row) keeps it, and every other row of that device is linked to it in
   ``raw_health_data_supersessions`` with the reason, plus an ``identity-merged`` event.
   Rows of different devices are never linked: each device keeps its own row (aliased by
   its fingerprint-specific key). A deleted-marked row with no live row of its device is a
   genuine deletion and stays as it is (``deletions_kept``).

A link never points at a superseded row and never supersedes a row that others point at
(no chains, no cycles); a group where that would be needed is left alone and counted in
``conflicts``. Rows whose stored ``source_start_time`` differs from the parsed start (written
by an older parser) are settled last, with the rows of their parsed start.

Rows are read in keyset order of ``(source_start_time, id)`` (index
``ix_raw_health_data_type_start``): every row of a v2 key has the same start, and a batch
always ends after the last row of its start time, so a key is never split. Each batch of the
second pass is one transaction holding the data type's exclusive lock (syncs of that data
type wait for it, then continue) with its own ``statement_timeout``; inserts are ``ON
CONFLICT DO NOTHING``, so the command is idempotent and can be stopped and run again at any
time (``--after`` skips the second pass up to a start time; the first pass always reads
everything, because a chain is decided over a source's whole history). Only one run at a
time: a second one refuses at once.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, insert, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.timeutil import as_utc
from app.google_health.data_types import METRICS_SCOPE, REGISTRY, DataTypeSpec, RecordKind
from app.models.identity import (
    IDENTITY_TABLE,
    MERGED_EVENT,
    RawHealthDataIdentity,
    RawHealthDataSupersession,
)
from app.models.raw_versions import RawHealthDataEvent, RawHealthDataVersion
from app.models.state import RawHealthData
from app.parsers.common import google_name
from app.parsers.identity import (
    RecordIdentity,
    instant,
    record_identity,
    source_label,
    supersession_reason,
)
from app.services.identity import aliases, among, successors
from app.services.source_chains import (
    CHAIN_GAP,
    Chains,
    Span,
    backfill_lock,
    eligible_predecessors,
    gap_seconds,
    insert_chain,
    load_chains,
    lock_data_type,
    write_spans,
)
from app.services.storage import dialect_insert

STATEMENT_TIMEOUT = "120s"


@dataclass
class BackfillStats:
    rows: int = 0
    # Rows with a Google id: they keep it, nothing to do.
    named: int = 0
    # Rows without any parsable time (no v2 identity; they keep their v1 id).
    unidentified: int = 0
    # v2 keys seen.
    identities: int = 0
    aliases_added: int = 0
    aliases_present: int = 0
    already_superseded: int = 0
    # Keys held by more than one device (each keeps its own row).
    distinct_sources: int = 0
    # Deleted-marked rows that hold their device's record with no live row of that device
    # beside them: genuine deletions, left as they are.
    deletions_kept: int = 0
    # Linked rows (inserted by this run) that carried a source-deletion mark (kept as history).
    superseded_marked: int = 0
    # Rows whose stored source_start_time differs from the parsed one (settled last).
    start_mismatches: int = 0
    # Rows left unlinked because linking them would chain two links or hide a live row
    # behind a deleted one.
    conflicts: int = 0
    # Links inserted by this run (a dry run: the links it would insert), per reason.
    linked: Counter = field(default_factory=Counter)
    # Per data type: the chains (this run's and earlier ones), source pairs that share keys
    # but stay apart, and fingerprints with more than one candidate predecessor.
    chains: list[dict] = field(default_factory=list)
    kept_apart: list[dict] = field(default_factory=list)
    ambiguous: list[dict] = field(default_factory=list)

    def add(self, other: BackfillStats) -> None:
        for name in self.__dataclass_fields__:
            if name == "linked":
                self.linked.update(other.linked)
            elif name in ("chains", "kept_apart", "ambiguous"):
                getattr(self, name).extend(getattr(other, name))
            else:
                setattr(self, name, getattr(self, name) + getattr(other, name))

    def as_dict(self) -> dict[str, Any]:
        values = {name: getattr(self, name) for name in self.__dataclass_fields__}
        values["linked"] = dict(sorted(self.linked.items()))
        values["superseded"] = sum(self.linked.values())
        return values


@dataclass
class _Row:
    raw_id: uuid.UUID
    google_data_point_id: str
    raw_json: Any
    first_seen_at: datetime
    last_seen_at: datetime
    deleted: bool
    identity: RecordIdentity

    @property
    def order(self) -> tuple:
        return (not self.deleted, self.last_seen_at, self.first_seen_at, str(self.raw_id))


def spec_for(data_type: str) -> DataTypeSpec:
    return REGISTRY.get(data_type) or DataTypeSpec(data_type, RecordKind.interval, METRICS_SCOPE)


_COLUMNS = (
    RawHealthData.id,
    RawHealthData.google_data_point_id,
    RawHealthData.raw_json,
    RawHealthData.source_start_time,
    RawHealthData.first_seen_at,
    RawHealthData.last_seen_at,
    RawHealthData.source_deleted_at,
    RawHealthData.payload_received_at,
)


async def _read_batch(
    session: AsyncSession,
    data_type: str,
    batch_size: int,
    cursor: tuple[datetime, uuid.UUID] | None,
    after: datetime | None,
) -> list:
    query = select(*_COLUMNS).where(
        RawHealthData.data_type == data_type, RawHealthData.source_start_time.is_not(None)
    )
    if cursor is not None:
        query = query.where(tuple_(RawHealthData.source_start_time, RawHealthData.id) > cursor)
    elif after is not None:
        query = query.where(RawHealthData.source_start_time >= after)
    order = (RawHealthData.source_start_time, RawHealthData.id)
    rows = list((await session.execute(query.order_by(*order).limit(batch_size))).tuples())
    if len(rows) < batch_size:
        return rows
    # Finish the last start time: an identity's rows all share it.
    last_start, last_id = rows[-1][3], rows[-1][0]
    rest = await session.execute(
        select(*_COLUMNS)
        .where(
            RawHealthData.data_type == data_type,
            RawHealthData.source_start_time == last_start,
            RawHealthData.id > last_id,
        )
        .order_by(RawHealthData.id)
    )
    rows.extend(rest.tuples())
    return rows


async def _prepare(session: AsyncSession, statement_timeout: str) -> None:
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text(f"SET LOCAL statement_timeout = '{statement_timeout}'"))
        # The alias table grows from empty during the run. A plan made while its
        # statistics still say "empty" filters every row of the data type against every
        # key (minutes per batch at millions of rows): plan each lookup with the current
        # statistics, which ANALYZE keeps up to date.
        await session.execute(text("SET LOCAL plan_cache_mode = force_custom_plan"))


# --- pass 1: sighting timelines and chains ------------------------------------------------


@dataclass
class _Timeline:
    spans: dict[str, Span] = field(default_factory=dict)
    # Unordered fingerprint pairs -> v2 keys they share.
    shared: Counter = field(default_factory=Counter)

    def sight(self, identity: RecordIdentity, data_source, first, last) -> None:
        span = self.spans.get(identity.source)
        if span is None:
            self.spans[identity.source] = Span(
                as_utc(first),
                as_utc(last),
                identity.stable,
                source_label(data_source),
                True,
                data_source,
            )
        else:
            span.widen(first, last)


async def _timeline(
    sessions: async_sessionmaker, spec: DataTypeSpec, batch_size: int, statement_timeout: str
) -> _Timeline:
    timeline = _Timeline()
    cursor: tuple[datetime, uuid.UUID] | None = None
    while True:
        async with sessions.begin() as session:
            await _prepare(session, statement_timeout)
            batch = await _read_batch(session, spec.name, batch_size, cursor, None)
            if not batch:
                break
            row_keys: dict[uuid.UUID, tuple[str, datetime]] = {}
            sources: dict[str, set[str]] = defaultdict(set)
            for raw_id, _, raw_json, _, first, last, _, received in batch:
                identity = record_identity(spec, raw_json)
                if google_name(raw_json) is not None or identity is None:
                    continue
                data_source = raw_json.get("dataSource")
                # The current payload arrived at payload_received_at (NULL before 0011: the
                # row never changed its source then, so its first sighting is the row's).
                timeline.sight(identity, data_source, received or first, last)
                row_keys[raw_id] = (identity.key, first)
                sources[identity.key].add(identity.source)
            if row_keys:
                versions = await session.execute(
                    select(
                        RawHealthDataVersion.raw_id,
                        RawHealthDataVersion.raw_json,
                        RawHealthDataVersion.received_at,
                        RawHealthDataVersion.last_seen_at,
                    ).where(among(session, RawHealthDataVersion.raw_id, list(row_keys)))
                )
                for raw_id, raw_json, received, last in versions.tuples():
                    identity = record_identity(spec, raw_json)
                    if identity is None:
                        continue
                    key, first = row_keys[raw_id]
                    timeline.sight(identity, raw_json.get("dataSource"), received or first, last)
                    if identity.key == key:
                        sources[key].add(identity.source)
        for found in sources.values():
            ordered = sorted(found)
            for index, left in enumerate(ordered):
                for right in ordered[index + 1 :]:
                    timeline.shared[(left, right)] += 1
        cursor = (batch[-1][3], batch[-1][0])
    return timeline


def _decide(chains: Chains, timeline: _Timeline) -> tuple[list[tuple[str, str, int]], list]:
    """New links by the rule of source_chains, oldest new fingerprint first."""
    neighbours: dict[str, set[str]] = defaultdict(set)
    for left, right in timeline.shared:
        neighbours[left].add(right)
        neighbours[right].add(left)
    links: list[tuple[str, str, int]] = []
    ambiguous = []
    for new in sorted(chains.spans, key=lambda name: (chains.spans[name].first, name)):
        if new not in neighbours:
            continue
        span = chains.spans[new]
        candidates = eligible_predecessors(chains, new, span.first, neighbours[new])
        if len(candidates) == 1:
            old = candidates[0]
            members = set(chains.members(old))
            shared = sum(
                count
                for (left, right), count in timeline.shared.items()
                if (left == new and right in members) or (right == new and left in members)
            )
            chains.add(old, new)
            links.append((old, new, shared))
        elif candidates:
            ambiguous.append(
                {
                    "source": span.label,
                    "candidates": sorted(chains.spans[name].label for name in candidates),
                }
            )
    return links, ambiguous


def _report(
    chains: Chains, timeline: _Timeline, new_links: set, ambiguous: list, stats: BackfillStats
) -> None:
    for old, new in sorted(chains.following.items()):
        before, after = chains.spans.get(old), chains.spans.get(new)
        stats.chains.append(
            {
                "from": before.label if before else old[:12],
                "to": after.label if after else new[:12],
                "gap_seconds": gap_seconds(before, after) if before and after else None,
                "decided": "this run" if (old, new) in new_links else "earlier",
                "valid": chains.valid(old, new),
            }
        )
    for (left, right), count in sorted(timeline.shared.items()):
        if chains.same_device(left, right):
            continue
        first, second = sorted((left, right), key=lambda name: chains.spans[name].first)
        gap = gap_seconds(chains.spans[first], chains.spans[second])
        stats.kept_apart.append(
            {
                "sources": [chains.spans[first].label, chains.spans[second].label],
                "shared_keys": count,
                "gap_seconds": gap,
                "why": (
                    "seen together (overlapping sightings)"
                    if gap <= CHAIN_GAP.total_seconds()
                    else "not the only candidate, or a chain already continues elsewhere"
                ),
            }
        )
    stats.ambiguous.extend(ambiguous)


async def _settle_chains(
    sessions: async_sessionmaker,
    spec: DataTypeSpec,
    timeline: _Timeline,
    stats: BackfillStats,
    *,
    dry_run: bool,
) -> Chains:
    """Merge the spans, decide and record the chains, under the exclusive lock."""
    now = datetime.now(UTC)
    async with sessions.begin() as session:
        await lock_data_type(session, spec.name, exclusive=True)
        chains = await load_chains(session, spec.name)
        for fingerprint, span in timeline.spans.items():
            stored = chains.spans.get(fingerprint)
            if stored is not None:
                span.widen(stored.first, stored.last)
            chains.spans[fingerprint] = span
        if not dry_run:
            await write_spans(session, spec.name, timeline.spans, now)
        links, ambiguous = _decide(chains, timeline)
        recorded: set[tuple[str, str]] = set()
        for old, new, shared in links:
            if dry_run:
                recorded.add((old, new))
                continue
            # _decide added the link to `chains` already; insert_chain checks the table.
            del chains.following[old], chains.preceding[new]
            if await insert_chain(
                session,
                spec.name,
                chains,
                old,
                new,
                decided_by="backfill",
                shared_keys=shared,
                now=now,
            ):
                recorded.add((old, new))
        _report(chains, timeline, recorded, ambiguous, stats)
    return chains


# --- pass 2: links ------------------------------------------------------------------------


@dataclass
class _Plan:
    aliases: dict[str, _Row] = field(default_factory=dict)
    links: list[tuple[_Row, _Row, str]] = field(default_factory=list)


async def _plan_batch(
    session: AsyncSession,
    spec: DataTypeSpec,
    rows: list[_Row],
    chains: Chains,
    stats: BackfillStats,
    *,
    count_groups: bool = True,
) -> _Plan:
    plan = _Plan()
    groups: dict[str, list[_Row]] = defaultdict(list)
    for row in rows:
        groups[row.identity.key].append(row)
    keys = {key for row in rows for key in (row.identity.key, row.identity.specific_key)}
    alias = await aliases(session, spec.name, keys)
    ids = [row.raw_id for row in rows]
    linked = set(
        (
            await session.scalars(
                select(RawHealthDataSupersession.old_raw_id).where(
                    among(session, RawHealthDataSupersession.old_raw_id, ids)
                )
            )
        ).all()
    )
    targets = set(
        (
            await session.scalars(
                select(RawHealthDataSupersession.new_raw_id).where(
                    among(session, RawHealthDataSupersession.new_raw_id, ids)
                )
            )
        ).all()
    )
    current = await successors(session, alias.values())
    aliased = {current[raw_id] for raw_id in alias.values()}

    for key, members in groups.items():
        if count_groups:
            stats.identities += 1
            stats.aliases_present += key in alias
        devices: dict[str, list[_Row]] = defaultdict(list)
        for row in members:
            devices[chains.root(row.identity.source)].append(row)
        if count_groups and len(devices) > 1:
            stats.distinct_sources += 1
        holders: list[_Row] = []
        for device in devices.values():
            candidates = [row for row in device if row.raw_id not in linked]
            if count_groups:
                stats.already_superseded += len(device) - len(candidates)
            if not candidates:
                continue
            pointed = [row for row in candidates if row.raw_id in targets]
            pool = (
                pointed
                or [row for row in candidates if row.raw_id in aliased and not row.deleted]
                or [row for row in candidates if not row.deleted]
                or candidates
            )
            holder = max(pool, key=lambda row: row.order)
            holders.append(holder)
            if count_groups and holder.deleted:
                stats.deletions_kept += 1
            for row in candidates:
                if row is holder:
                    continue
                if row.raw_id in targets or (holder.deleted and not row.deleted):
                    stats.conflicts += 1
                    continue
                reason = supersession_reason(spec, row.raw_json, holder.raw_json)
                plan.links.append((row, holder, reason))
            if len(devices) > 1:
                for row in device:
                    if row.identity.specific_key not in alias:
                        plan.aliases.setdefault(row.identity.specific_key, holder)
        if holders and key not in alias:
            plan.aliases[key] = max(holders, key=lambda row: row.order)
    return plan


async def _write_plan(
    session: AsyncSession, spec: DataTypeSpec, plan: _Plan, stats: BackfillStats, *, dry_run: bool
) -> None:
    if dry_run:
        for row, _, reason in plan.links:
            stats.linked[reason] += 1
            stats.superseded_marked += row.deleted
        stats.aliases_added += len(plan.aliases)
        return
    if plan.aliases:
        statement = (
            dialect_insert(session, RawHealthDataIdentity.__table__)
            .values(
                [
                    {
                        "data_type": spec.name,
                        "identity_key": key,
                        "raw_id": row.raw_id,
                        "google_data_point_id": row.google_data_point_id,
                    }
                    for key, row in sorted(plan.aliases.items())
                ]
            )
            .on_conflict_do_nothing(index_elements=["data_type", "identity_key"])
            .returning(RawHealthDataIdentity.identity_key)
        )
        stats.aliases_added += len((await session.execute(statement)).scalars().all())
    if not plan.links:
        return
    table = RawHealthDataSupersession.__table__
    values = [
        {
            "old_raw_id": old.raw_id,
            "new_raw_id": new.raw_id,
            "data_type": spec.name,
            "old_google_data_point_id": old.google_data_point_id,
            "new_google_data_point_id": new.google_data_point_id,
            "reason": reason,
        }
        for old, new, reason in sorted(plan.links, key=lambda link: str(link[0].raw_id))
    ]
    # RETURNING names exactly the links this run added.
    inserted: set[uuid.UUID] = set()
    for offset in range(0, len(values), 1000):
        statement = (
            dialect_insert(session, table)
            .values(values[offset : offset + 1000])
            .on_conflict_do_nothing(index_elements=["old_raw_id"])
            .returning(table.c.old_raw_id)
        )
        inserted.update((await session.execute(statement)).scalars().all())
    for row, _, reason in plan.links:
        if row.raw_id in inserted:
            stats.linked[reason] += 1
            stats.superseded_marked += row.deleted
    # One identity-merged event per row, ever: a link inserted again after a downgrade
    # (-x allow-identity-loss=1) and upgrade finds the event of the first one.
    merged = set(
        (
            await session.scalars(
                select(RawHealthDataEvent.raw_id).where(
                    RawHealthDataEvent.event == MERGED_EVENT,
                    among(session, RawHealthDataEvent.raw_id, sorted(inserted)),
                )
            )
        ).all()
        if inserted
        else ()
    )
    now = datetime.now(UTC)
    events = [
        {
            "raw_id": old.raw_id,
            "data_type": spec.name,
            "google_data_point_id": old.google_data_point_id,
            "event": MERGED_EVENT,
            "occurred_at": now,
            "previous_deleted_at": None,
        }
        for old, _, _ in plan.links
        if old.raw_id in inserted and old.raw_id not in merged
    ]
    if events:
        await session.execute(insert(RawHealthDataEvent), events)


def _rows_of(
    spec: DataTypeSpec, batch: Sequence, stats: BackfillStats | None
) -> tuple[list[_Row], list[_Row]]:
    """(rows to settle, rows whose stored start differs from the parsed one)."""
    rows: list[_Row] = []
    mismatched: list[_Row] = []
    for raw_id, google_id, raw_json, start, first, last, deleted, _ in batch:
        if stats is not None:
            stats.rows += 1
        if google_name(raw_json) is not None:
            if stats is not None:
                stats.named += 1
            continue
        identity = record_identity(spec, raw_json)
        if identity is None:
            if stats is not None:
                stats.unidentified += 1
            continue
        row = _Row(
            raw_id, google_id, raw_json, as_utc(first), as_utc(last), deleted is not None, identity
        )
        if instant(identity.start) != instant(start):
            mismatched.append(row)
        else:
            rows.append(row)
    return rows, mismatched


async def _batch_chains(session: AsyncSession, data_type: str, planned: Chains) -> Chains:
    """The recorded chains now (a sync may have added one), plus a dry run's planned ones."""
    chains = await load_chains(session, data_type)
    for old, new in planned.following.items():
        if old not in chains.following and new not in chains.preceding:
            chains.add(old, new)
    for fingerprint, span in planned.spans.items():
        chains.spans.setdefault(fingerprint, span)
    return chains


async def backfill_data_type(
    sessions: async_sessionmaker,
    data_type: str,
    *,
    batch_size: int = 5_000,
    dry_run: bool = False,
    after: datetime | None = None,
    statement_timeout: str = STATEMENT_TIMEOUT,
    progress: Callable[[str, BackfillStats, datetime], None] | None = None,
) -> BackfillStats:
    spec = spec_for(data_type)
    total = BackfillStats()
    timeline = await _timeline(sessions, spec, batch_size, statement_timeout)
    planned = await _settle_chains(sessions, spec, timeline, total, dry_run=dry_run)
    cursor: tuple[datetime, uuid.UUID] | None = None
    batches = 0
    deferred: list[_Row] = []
    while True:
        stats = BackfillStats()
        async with sessions.begin() as session:
            await lock_data_type(session, data_type, exclusive=True)
            await _prepare(session, statement_timeout)
            if (
                not dry_run
                and session.get_bind().dialect.name == "postgresql"
                and ((batches & (batches - 1)) == 0 or batches % 50 == 0)
            ):
                await session.execute(text(f"ANALYZE {IDENTITY_TABLE}"))
            batches += 1
            batch = await _read_batch(session, data_type, batch_size, cursor, after)
            if not batch:
                break
            rows, mismatched = _rows_of(spec, batch, stats)
            stats.start_mismatches += len(mismatched)
            deferred.extend(mismatched)
            chains = await _batch_chains(session, data_type, planned)
            plan = await _plan_batch(session, spec, rows, chains, stats)
            await _write_plan(session, spec, plan, stats, dry_run=dry_run)
        cursor = (batch[-1][3], batch[-1][0])
        total.add(stats)
        if progress is not None:
            progress(data_type, total, as_utc(cursor[0]))
    for offset in range(0, len(deferred), batch_size):
        stats = BackfillStats()
        await _settle_deferred(
            sessions, spec, deferred[offset : offset + batch_size], planned, stats, dry_run=dry_run
        )
        total.add(stats)
    async with sessions() as session:
        undated = await session.scalar(
            select(func.count()).where(
                RawHealthData.data_type == data_type, RawHealthData.source_start_time.is_(None)
            )
        )
    total.unidentified += undated or 0
    return total


async def _settle_deferred(
    sessions: async_sessionmaker,
    spec: DataTypeSpec,
    deferred: list[_Row],
    planned: Chains,
    stats: BackfillStats,
    *,
    dry_run: bool,
) -> None:
    """Rows with a stale stored start, with the rows of their parsed start."""
    starts = sorted({row.identity.start for row in deferred if row.identity.start is not None})
    async with sessions.begin() as session:
        await lock_data_type(session, spec.name, exclusive=True)
        found = await session.execute(
            select(*_COLUMNS).where(
                RawHealthData.data_type == spec.name,
                among(session, RawHealthData.source_start_time, starts),
            )
        )
        rows, _ = _rows_of(spec, found.tuples().all(), None)
        keys = {row.identity.key for row in deferred}
        rows = [row for row in rows if row.identity.key in keys]
        known = {row.raw_id for row in rows}
        rows.extend(row for row in deferred if row.raw_id not in known)
        chains = await _batch_chains(session, spec.name, planned)
        plan = await _plan_batch(session, spec, rows, chains, stats, count_groups=False)
        await _write_plan(session, spec, plan, stats, dry_run=dry_run)


async def data_types_in_archive(sessions: async_sessionmaker) -> list[str]:
    async with sessions() as session:
        return sorted((await session.scalars(select(RawHealthData.data_type).distinct())).all())


async def identity_backfill(
    sessions: async_sessionmaker,
    *,
    data_types: Sequence[str] | None = None,
    batch_size: int = 5_000,
    dry_run: bool = False,
    after: datetime | None = None,
    statement_timeout: str = STATEMENT_TIMEOUT,
    progress: Callable[[str, BackfillStats, datetime], None] | None = None,
) -> dict[str, BackfillStats]:
    """Raises ``BackfillRunning`` when another run holds the single-run lock."""
    async with backfill_lock(sessions):
        names = list(data_types) if data_types else await data_types_in_archive(sessions)
        results = {
            name: await backfill_data_type(
                sessions,
                name,
                batch_size=batch_size,
                dry_run=dry_run,
                after=after,
                statement_timeout=statement_timeout,
                progress=progress,
            )
            for name in names
        }
        if not dry_run:
            async with sessions.begin() as session:
                if session.get_bind().dialect.name == "postgresql":
                    # Fresh statistics for the lookups every stored page makes from now on.
                    await session.execute(
                        text(
                            "ANALYZE raw_health_data_identity, raw_health_data_supersessions, "
                            "raw_source_chains, raw_source_sightings"
                        )
                    )
    return results
