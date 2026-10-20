"""``health-mirror identity-backfill``: v2 aliases and supersession links for archived rows.

Rows stored before migration 20261021_0012 have no alias, and rows archived twice under
older ids of the same record (a device rename, a package suffix change, civil times added
to an interval, a growing interval) are separate live rows. For every row without a Google
id this command computes the v2 identity from ``raw_json`` and, per identity:

* adds the alias ``identity key -> row that holds the record`` (the alias already there wins);
* links every other row of the identity to that row in ``raw_health_data_supersessions``,
  with the reason, and appends an ``identity-merged`` event for the superseded row.

The holder is the newest live row (largest ``last_seen_at``; a deleted-marked row only if
the identity has no live row). Rows with the same v2 key but another fingerprint (display
name, package, growing end) are one identity only when they were never returned together:
if their sightings (``first_seen_at`` .. ``last_seen_at``) overlap, they are distinct
sources and each keeps its own row, aliased by its specific key. A deleted-marked row with
no other row of its identity is left as it is: that is a real deletion.

``raw_health_data`` is only read. Rows are read in keyset order of
``(source_start_time, id)`` (index ``ix_raw_health_data_type_start``): every row of an
identity has the same start, and a batch always ends after the last row of its start time,
so an identity is never split. Each batch is one transaction with its own
``statement_timeout``; inserts are ``ON CONFLICT DO NOTHING``, so the command is idempotent
and can be stopped and run again at any time (``--after`` skips what is done).
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import combinations
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
from app.models.raw_versions import RawHealthDataEvent
from app.models.state import RawHealthData
from app.parsers.common import google_name
from app.parsers.identity import RecordIdentity, instant, record_identity, supersession_reason
from app.services.identity import aliases, among, successors
from app.services.storage import dialect_insert

STATEMENT_TIMEOUT = "120s"


@dataclass
class BackfillStats:
    rows: int = 0
    # Rows with a Google id: they keep it, nothing to do.
    named: int = 0
    # Rows without any parsable time (no v2 identity; they keep their v1 id).
    unidentified: int = 0
    identities: int = 0
    aliases_added: int = 0
    aliases_present: int = 0
    already_superseded: int = 0
    # Identities whose rows were returned together with another fingerprint.
    distinct_sources: int = 0
    # Deleted-marked rows that are the only row of their identity: real deletions, kept.
    deletions_kept: int = 0
    # Superseded rows that carried a source-deletion mark (kept as history).
    superseded_marked: int = 0
    # Identities whose alias points to a row outside the batch (left alone; see docstring).
    alias_elsewhere: int = 0
    # Rows whose stored source_start_time differs from the parsed one (stale column).
    start_mismatches: int = 0
    linked: Counter = field(default_factory=Counter)

    def add(self, other: BackfillStats) -> None:
        for name in self.__dataclass_fields__:
            if name == "linked":
                self.linked.update(other.linked)
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
        return (self.last_seen_at, self.first_seen_at, str(self.raw_id))


def spec_for(data_type: str) -> DataTypeSpec:
    return REGISTRY.get(data_type) or DataTypeSpec(data_type, RecordKind.interval, METRICS_SCOPE)


def _overlap(left: Sequence[_Row], right: Sequence[_Row]) -> bool:
    left_first = min(row.first_seen_at for row in left)
    left_last = max(row.last_seen_at for row in left)
    right_first = min(row.first_seen_at for row in right)
    right_last = max(row.last_seen_at for row in right)
    return left_first <= right_last and right_first <= left_last


@dataclass
class _Plan:
    aliases: dict[str, _Row] = field(default_factory=dict)
    links: list[tuple[_Row, _Row, str]] = field(default_factory=list)


async def _plan_batch(
    session: AsyncSession, spec: DataTypeSpec, rows: list[_Row], stats: BackfillStats
) -> _Plan:
    plan = _Plan()
    groups: dict[str, list[_Row]] = defaultdict(list)
    for row in rows:
        groups[row.identity.key].append(row)
    keys = {key for row in rows for key in (row.identity.key, row.identity.specific_key)}
    alias = await aliases(session, spec.name, keys)
    linked = set(
        (
            await session.scalars(
                select(RawHealthDataSupersession.old_raw_id).where(
                    among(
                        session,
                        RawHealthDataSupersession.old_raw_id,
                        [row.raw_id for row in rows],
                    )
                )
            )
        ).all()
    )
    current = await successors(session, alias.values())

    def settle(key: str, members: list[_Row]) -> _Row | None:
        """Holder of one identity; plans its alias and the links of its other rows."""
        stats.identities += 1
        candidates = [row for row in members if row.raw_id not in linked]
        stats.already_superseded += len(members) - len(candidates)
        if not candidates:
            return None
        by_id = {row.raw_id: row for row in candidates}
        aliased = by_id.get(current[alias[key]]) if key in alias else None
        if (
            key in alias
            and aliased is None
            and current[alias[key]] not in {row.raw_id for row in members}
        ):
            stats.alias_elsewhere += 1
            return None
        live = [row for row in candidates if not row.deleted]
        if aliased is not None and not aliased.deleted:
            holder = aliased
        elif live:
            holder = max(live, key=lambda row: row.order)
        else:
            holder = aliased or max(candidates, key=lambda row: row.order)
        if key in alias:
            stats.aliases_present += 1
        else:
            plan.aliases[key] = holder
        if len(candidates) == 1 and holder.deleted:
            stats.deletions_kept += 1
        for row in candidates:
            if row is not holder:
                plan.links.append(
                    (row, holder, supersession_reason(spec, row.raw_json, holder.raw_json))
                )
        return holder

    for key, members in groups.items():
        by_fingerprint: dict[tuple, list[_Row]] = defaultdict(list)
        for row in members:
            by_fingerprint[row.identity.fingerprint].append(row)
        parts = list(by_fingerprint.values())
        if not any(_overlap(left, right) for left, right in combinations(parts, 2)):
            settle(key, members)
            continue
        # Returned together: distinct sources, one identity each (by specific key).
        stats.distinct_sources += 1
        holders = [settle(part[0].identity.specific_key, part) for part in parts]
        newest = max(
            (row for row in holders if row is not None), key=lambda row: row.order, default=None
        )
        if newest is not None and key not in alias:
            plan.aliases.setdefault(key, newest)
    return plan


async def _write_plan(
    session: AsyncSession, spec: DataTypeSpec, plan: _Plan, stats: BackfillStats, *, dry_run: bool
) -> None:
    for row, _, reason in plan.links:
        stats.linked[reason] += 1
        stats.superseded_marked += row.deleted
    stats.aliases_added += len(plan.aliases)
    if dry_run:
        return
    if plan.aliases:
        statement = dialect_insert(session, RawHealthDataIdentity.__table__).on_conflict_do_nothing(
            index_elements=["data_type", "identity_key"]
        )
        await session.execute(
            statement,
            [
                {
                    "data_type": spec.name,
                    "identity_key": key,
                    "raw_id": row.raw_id,
                    "google_data_point_id": row.google_data_point_id,
                }
                for key, row in sorted(plan.aliases.items())
            ],
        )
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
    # One multi-row statement per chunk: RETURNING names exactly the links this run added
    # (a concurrent run may have added some first).
    inserted: set[uuid.UUID] = set()
    for offset in range(0, len(values), 1000):
        statement = (
            dialect_insert(session, table)
            .values(values[offset : offset + 1000])
            .on_conflict_do_nothing(index_elements=["old_raw_id"])
            .returning(table.c.old_raw_id)
        )
        inserted.update((await session.execute(statement)).scalars().all())
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
        if old.raw_id in inserted
    ]
    if events:
        await session.execute(insert(RawHealthDataEvent), events)


_COLUMNS = (
    RawHealthData.id,
    RawHealthData.google_data_point_id,
    RawHealthData.raw_json,
    RawHealthData.source_start_time,
    RawHealthData.first_seen_at,
    RawHealthData.last_seen_at,
    RawHealthData.source_deleted_at,
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
    cursor: tuple[datetime, uuid.UUID] | None = None
    batches = 0
    while True:
        stats = BackfillStats()
        async with sessions.begin() as session:
            postgresql = session.get_bind().dialect.name == "postgresql"
            if postgresql:
                await session.execute(text(f"SET LOCAL statement_timeout = '{statement_timeout}'"))
                # The alias table grows from empty during the run. A plan made while its
                # statistics still say "empty" filters every row of the data type against
                # every key (minutes per batch at millions of rows): plan each lookup with
                # the current statistics, which ANALYZE below keeps up to date.
                await session.execute(text("SET LOCAL plan_cache_mode = force_custom_plan"))
                if not dry_run and ((batches & (batches - 1)) == 0 or batches % 50 == 0):
                    await session.execute(text(f"ANALYZE {IDENTITY_TABLE}"))
            batches += 1
            batch = await _read_batch(session, data_type, batch_size, cursor, after)
            if not batch:
                break
            rows: list[_Row] = []
            for raw_id, google_id, raw_json, start, first, last, deleted in batch:
                stats.rows += 1
                if google_name(raw_json) is not None:
                    stats.named += 1
                    continue
                identity = record_identity(spec, raw_json)
                if identity is None:
                    stats.unidentified += 1
                    continue
                if instant(identity.start) != instant(start):
                    stats.start_mismatches += 1
                rows.append(
                    _Row(
                        raw_id,
                        google_id,
                        raw_json,
                        as_utc(first),
                        as_utc(last),
                        deleted is not None,
                        identity,
                    )
                )
            plan = await _plan_batch(session, spec, rows, stats)
            await _write_plan(session, spec, plan, stats, dry_run=dry_run)
        cursor = (batch[-1][3], batch[-1][0])
        total.add(stats)
        if progress is not None:
            progress(data_type, total, as_utc(cursor[0]))
    async with sessions() as session:
        undated = await session.scalar(
            select(func.count()).where(
                RawHealthData.data_type == data_type, RawHealthData.source_start_time.is_(None)
            )
        )
    total.unidentified += undated or 0
    return total


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
                    text("ANALYZE raw_health_data_identity, raw_health_data_supersessions")
                )
    return results
