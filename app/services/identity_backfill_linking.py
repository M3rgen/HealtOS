"""Transactional writes and batch link settlement for identity backfills."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.timeutil import as_utc
from app.google_health.data_types import DataTypeSpec
from app.models.identity import (
    IDENTITY_TABLE,
    MERGED_EVENT,
    RawHealthDataIdentity,
    RawHealthDataSupersession,
)
from app.models.raw_versions import RawHealthDataEvent
from app.models.state import RawHealthData
from app.parsers.identity import reading
from app.services.identity import among
from app.services.identity_backfill_agreement import _record_agreement
from app.services.identity_backfill_planning import _Plan, _plan_batch
from app.services.identity_backfill_reading import _COLUMNS, _read_batch, _rows_at, _rows_of
from app.services.identity_backfill_state import INSERT_CHUNK, Exclusive, Notice, _chunked
from app.services.identity_backfill_stats import BackfillStats
from app.services.source_chains import Chains, load_chains
from app.services.storage import dialect_insert


async def _write_plan(
    session: AsyncSession, spec: DataTypeSpec, plan: _Plan, stats: BackfillStats, *, dry_run: bool
) -> None:
    if dry_run:
        for row, _, reason in plan.links:
            stats.linked[reason] += 1
            stats.superseded_marked += row.deleted
        stats.aliases_added += len(plan.aliases)
        return
    await _record_agreement(session, spec.name, stats.agreement_keys)
    if plan.aliases:
        values = [
            {
                "data_type": spec.name,
                "identity_key": key,
                "raw_id": row.raw_id,
                "google_data_point_id": row.google_data_point_id,
            }
            for key, row in sorted(plan.aliases.items())
        ]
        table = RawHealthDataIdentity.__table__
        for offset in range(0, len(values), INSERT_CHUNK):
            statement = (
                dialect_insert(session, table)
                .values(values[offset : offset + INSERT_CHUNK])
                .on_conflict_do_nothing(index_elements=["data_type", "identity_key"])
                .returning(table.c.identity_key)
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
            "old_fingerprint": old.identity.source,
            "new_fingerprint": new.identity.source,
            "same_reading": reading(spec, old.raw_json) == reading(spec, new.raw_json),
            "new_payload_hash": new.payload_hash,
        }
        for old, new, reason in sorted(
            plan.links, key=lambda link: (str(link[0].raw_id), str(link[1].raw_id))
        )
    ]
    # RETURNING names exactly the links this run added.
    inserted: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for offset in range(0, len(values), INSERT_CHUNK):
        statement = (
            dialect_insert(session, table)
            .values(values[offset : offset + INSERT_CHUNK])
            .on_conflict_do_nothing(index_elements=["old_raw_id", "new_raw_id"])
            .returning(table.c.old_raw_id, table.c.new_raw_id)
        )
        inserted.update((await session.execute(statement)).tuples().all())
    for row, holder, reason in plan.links:
        if (row.raw_id, holder.raw_id) in inserted:
            stats.linked[reason] += 1
            stats.superseded_marked += row.deleted
    # One identity-merged event per row, ever: a row linked again (or a link inserted again
    # after a downgrade with -x allow-identity-loss=1 and an upgrade) finds the first one.
    olds = {old for old, _ in inserted}
    merged: set[uuid.UUID] = set()
    for chunk in _chunked(sorted(olds)):
        merged.update(
            (
                await session.scalars(
                    select(RawHealthDataEvent.raw_id).where(
                        RawHealthDataEvent.event == MERGED_EVENT,
                        among(session, RawHealthDataEvent.raw_id, chunk),
                    )
                )
            ).all()
        )
    now = datetime.now(UTC)
    events = []
    for old, _, _ in plan.links:
        if old.raw_id in olds and old.raw_id not in merged:
            merged.add(old.raw_id)
            events.append(
                {
                    "raw_id": old.raw_id,
                    "data_type": spec.name,
                    "google_data_point_id": old.google_data_point_id,
                    "event": MERGED_EVENT,
                    "occurred_at": now,
                    "previous_deleted_at": None,
                }
            )
    for chunk in _chunked(events):
        await session.execute(insert(RawHealthDataEvent), list(chunk))


async def _batch_chains(session: AsyncSession, data_type: str, planned: Chains) -> Chains:
    """The recorded chains now, plus a dry run's planned links, evidence and decisions."""
    chains = await load_chains(session, data_type)
    for link in planned.links - chains.links:
        chains.links.add(link)
        chains.decided_by[link] = planned.decided_by.get(link, "reconciler")
    for (left, right), items in planned.evidence.items():
        for item in items:
            chains.add_evidence(left, right, item.key, item.seen)
    chains.chain_decisions.update(planned.chain_decisions)
    chains.pair_decisions.update(planned.pair_decisions)
    chains.refuted_events |= planned.refuted_events
    for fingerprint, span in planned.spans.items():
        chains.spans.setdefault(fingerprint, span)
    chains._changed()
    return chains


async def _settle_starts(
    session: AsyncSession,
    spec: DataTypeSpec,
    starts: Sequence[datetime],
    extra: Sequence[uuid.UUID],
    planned: Chains,
    stats: BackfillStats,
    *,
    dry_run: bool,
    count: bool,
    rows_at: Callable[..., Awaitable[list]] = _rows_at,
    write_plan: Callable[..., Awaitable[None]] = _write_plan,
) -> None:
    """Every row stored at one of ``starts``, plus the rows ``extra`` (by id: rows whose
    stored start is stale), settled together (keys of the extra rows only, then)."""
    found = await rows_at(session, spec, starts)
    rows, mismatched = _rows_of(spec, found, stats if count else None)
    rows.extend(mismatched)
    if extra:
        by_id = (
            await session.execute(
                select(*_COLUMNS).where(among(session, RawHealthData.id, sorted(extra)))
            )
        ).tuples()
        stale, stale_mismatched = _rows_of(spec, by_id.all(), None)
        keys = {row.identity.match for row in (*stale, *stale_mismatched)}
        known = {row.raw_id for row in rows}
        rows = [row for row in rows if row.identity.match in keys]
        rows.extend(row for row in (*stale, *stale_mismatched) if row.raw_id not in known)
    chains = await _batch_chains(session, spec.name, planned)
    plan = await _plan_batch(session, spec, rows, chains, stats, count_groups=count)
    await write_plan(session, spec, plan, stats, dry_run=dry_run)


async def _link_pass(
    sessions: async_sessionmaker,
    spec: DataTypeSpec,
    planned: Chains,
    total: BackfillStats,
    *,
    exclusively: Exclusive,
    read_batch: Callable[..., Awaitable[list]] = _read_batch,
    write_plan: Callable[..., Awaitable[None]] = _write_plan,
    batch_size: int,
    dry_run: bool,
    after: datetime | None,
    statement_timeout: str,
    progress: Callable[[str, BackfillStats, datetime], None] | None,
    notice: Notice | None,
) -> None:
    """Pass 2 over every row of the data type (from ``after``), batch by batch."""
    cursor: tuple[datetime, uuid.UUID] | None = None
    batches = 0
    while True:
        stats = BackfillStats(mode=total.mode)
        analyze = not dry_run and ((batches & (batches - 1)) == 0 or batches % 50 == 0)
        batches += 1

        async def work(
            session: AsyncSession, stats=stats, analyze=analyze, cursor=cursor
        ) -> tuple[datetime, uuid.UUID] | None:
            if analyze and session.get_bind().dialect.name == "postgresql":
                await session.execute(text(f"ANALYZE {IDENTITY_TABLE}"))
            batch = await read_batch(session, spec.name, batch_size, cursor, after)
            if not batch:
                return None
            # Stale-start rows were noted by id in pass 1 (also those before ``after``) and
            # are settled last, with the rows of their parsed start.
            rows, _ = _rows_of(spec, batch, stats)
            chains = await _batch_chains(session, spec.name, planned)
            plan = await _plan_batch(session, spec, rows, chains, stats)
            await write_plan(session, spec, plan, stats, dry_run=dry_run)
            return (batch[-1][3], batch[-1][0])

        reached = await exclusively(
            sessions, spec.name, work, statement_timeout, notice, dry_run=dry_run
        )
        if reached is None:
            break
        cursor = reached
        total.add(stats)
        if progress is not None:
            progress(spec.name, total, as_utc(reached[0]))
