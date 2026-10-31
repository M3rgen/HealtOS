"""The identity reconciler (``health-mirror identity-backfill``): chains, aliases and links.

The write path (app/services/identity.py) never decides that two source fingerprints are one
device; it stores an unknown fingerprint's records on rows of their own. This module (and the
owner's ``identity-decide``, app/services/identity_decisions.py) decides, and links what was
decided. Per data type:

1. **Chains.** A *full* run first reads every row without a Google id and every earlier
   version (``raw_health_data`` is only read): each gives a sighting of its source
   fingerprint (received .. last seen), and two rows of one key that one page stored or last
   saw together, with two different readings, are evidence of two devices
   (``raw_source_pairs``). The spans are merged into ``raw_source_sightings`` (complete from
   now on). An *incremental* run (nightly) uses the sightings and evidence as the write path
   keeps them. Either way links whose two fingerprints became established distinct get a
   ``refuted`` event, then the chains are decided per stable class by the rule of
   app/services/source_chains.py and appended to ``raw_source_chains``, and the devices that
   changed are logged (``raw_source_devices``, what readers count by); the cases the rule
   leaves open are listed in the report (``ambiguous``), every chain not yet reviewed by the
   owner under ``review``, pairs that look like a rename although evidence made them two
   devices under ``review_distinct``.
2. **Links.** Per record (match key: the v2 key without the optional device fields, so a form with
   ``formFactor`` added is compared with the form without), the rows are grouped by device (one
   fingerprint, or several joined by live chain links). Rows superseded by a link in force are
   settled; a row whose links are no longer in force (its chain was rejected or refuted) is
   settled again like any other. Within one device the row that holds the record (a live row
   first, one that already holds others first, then the newest) keeps it, and every other row of
   that device is linked to it in ``raw_health_data_supersessions`` with the reason and both
   fingerprints, plus an ``identity-merged`` event (once per row) - a row of *another
   fingerprint* of the device only when it measured the same (``reading``): a value of its own
   is never hidden (``kept_differing``). The rows a linked row held are linked to the holder
   too, so two devices joined later end with one holder. Rows of different devices are never
   linked, nor is a live row linked to a deleted one or a link made that would close a cycle
   (``conflicts``: both rows count, listed in status). Every row whose own id does not name its
   key and fingerprint gets an alias (``synthetic:v2s:`` rows need none). A deleted-marked row
   with no live row of its device stays as it is: a genuine deletion (``deletions_kept``), or a
   *suspicious* one when another kindred fingerprint not established distinct holds the
   record live (``deletions_suspicious``: possibly a rename nobody decided, listed for review).

A full run reads all rows in keyset order of ``(source_start_time, id)`` (index
``ix_raw_health_data_type_start``): every row of a v2 key has the same start, and a batch
always ends after the last row of its start time, so a key is never split. Rows whose stored
``source_start_time`` differs from the parsed start (an older parser) are noted by id during
the first pass and settled last with the rows of their parsed start - also when ``--after``
skips the rest of the second pass. An incremental run reads only the start times of rows
created or given a new payload (``updated_at`` and ``payload_received_at``) since its checkpoint
minus ``OVERLAP``, and, when it decided a new link, since the new fingerprint's first sighting;
they are streamed, never held in memory at once. A data type that never had a full run gets
its first full pass from the incremental run, and a data type that fails is reported while
the others still run.

Each batch of the second pass is one transaction holding the data type's exclusive lock
(writers of that data type wait for it, then continue): it tries for it without queueing for
``LOCK_TIMEOUT`` and then waits in the queue at most ``QUEUE_TIMEOUT``, and once it holds it
waits at most ``ROW_LOCK_TIMEOUT`` for any row lock of another transaction; then it backs off
and retries, so it never queues every writer behind a long sync or import. A dry run takes
no lock at all and writes nothing. Inserts are ``ON CONFLICT DO NOTHING`` in chunks (far below
PostgreSQL's 32,767 parameters), so a run is idempotent and can be stopped and run again at
any time. Only one run at a time: a second one refuses at once.

Run orchestration and the replaceable lock/reader/write hooks stay at this entry point.
Timeline/chain reconciliation, link planning/writing, agreement and checkpoint bookkeeping
live in the corresponding identity_backfill_* modules, which never import this executor.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from functools import wraps

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import event
from app.core.timeutil import as_utc
from app.google_health.data_types import METRICS_SCOPE, REGISTRY, DataTypeSpec, RecordKind
from app.models.state import RawHealthData
from app.services import identity_backfill_chains as chains
from app.services import identity_backfill_linking as linking
from app.services import source_chains
from app.services.identity_backfill_agreement import _agreement_of as _agreement_of
from app.services.identity_backfill_agreement import _count_shared as _count_shared
from app.services.identity_backfill_agreement import _mark_counted as _mark_counted
from app.services.identity_backfill_agreement import _record_agreement as _record_agreement
from app.services.identity_backfill_agreement import _review_agreeing as _review_agreeing
from app.services.identity_backfill_agreement import _summed as _summed
from app.services.identity_backfill_agreement import _tally_agreement as _tally_agreement
from app.services.identity_backfill_agreement import summed_agreement as summed_agreement
from app.services.identity_backfill_linking import _batch_chains as _batch_chains
from app.services.identity_backfill_linking import _write_plan as _write_plan
from app.services.identity_backfill_planning import _Link as _Link
from app.services.identity_backfill_planning import _links_of as _links_of
from app.services.identity_backfill_planning import _Plan as _Plan
from app.services.identity_backfill_planning import _plan_batch as _plan_batch
from app.services.identity_backfill_reading import _COLUMNS as _COLUMNS
from app.services.identity_backfill_reading import _EPOCH as _EPOCH
from app.services.identity_backfill_reading import _collect_starts as _collect_starts
from app.services.identity_backfill_reading import _from_micros as _from_micros
from app.services.identity_backfill_reading import _micros as _micros
from app.services.identity_backfill_reading import _read_batch as _read_batch
from app.services.identity_backfill_reading import _Row as _Row
from app.services.identity_backfill_reading import _rows_at as _rows_at
from app.services.identity_backfill_reading import _rows_of as _rows_of
from app.services.identity_backfill_reporting import _full as _full
from app.services.identity_backfill_reporting import _report as _report
from app.services.identity_backfill_reporting import _Timeline as _Timeline
from app.services.identity_backfill_reporting import _together as _together
from app.services.identity_backfill_state import INSERT_CHUNK as INSERT_CHUNK
from app.services.identity_backfill_state import OVERLAP as OVERLAP
from app.services.identity_backfill_state import DeviceChange as DeviceChange
from app.services.identity_backfill_state import Notice as Notice
from app.services.identity_backfill_state import _chunked as _chunked
from app.services.identity_backfill_state import _deferred_of as _deferred_of
from app.services.identity_backfill_state import _owe_settlement as _owe_settlement
from app.services.identity_backfill_state import _prepare as _prepare
from app.services.identity_backfill_state import _reconcile_row as _reconcile_row
from app.services.identity_backfill_state import _reconcile_state as _reconcile_state
from app.services.identity_backfill_state import _save_state as _save_state
from app.services.identity_backfill_state import _settled as _settled
from app.services.identity_backfill_stats import SUMMARY_ITEMS as SUMMARY_ITEMS
from app.services.identity_backfill_stats import BackfillStats as BackfillStats
from app.services.identity_backfill_stats import _fingerprints as _fingerprints
from app.services.source_chains import (
    LOCK_TIMEOUT,
    Chains,
    LockBusy,
    backfill_lock,
    describe,
    lock_data_type,
)

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT = "120s"
# A busy batch retries with bounded exponential backoff.
LOCK_ATTEMPTS = 8
_LOCK_NOT_AVAILABLE = "55P03"


# Resolve callbacks on each call so existing callers can still replace the legacy hooks.
@wraps(chains._timeline)
async def _timeline(*args, **kwargs) -> _Timeline:
    return await chains._timeline(*args, **kwargs, read_batch=_read_batch)


@wraps(chains._settle_chains)
async def _settle_chains(*args, **kwargs) -> tuple[Chains, DeviceChange]:
    return await chains._settle_chains(*args, **kwargs, exclusively=_exclusively)


@wraps(linking._settle_starts)
async def _settle_starts(*args, **kwargs) -> None:
    return await linking._settle_starts(*args, **kwargs, rows_at=_rows_at, write_plan=_write_plan)


@wraps(linking._link_pass)
async def _link_pass(*args, **kwargs) -> None:
    return await linking._link_pass(
        *args, **kwargs, exclusively=_exclusively, read_batch=_read_batch, write_plan=_write_plan
    )


class LockUnavailable(RuntimeError):
    """A batch could not get its data type's exclusive lock."""


def spec_for(data_type: str) -> DataTypeSpec:
    return REGISTRY.get(data_type) or DataTypeSpec(data_type, RecordKind.interval, METRICS_SCOPE)


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _sqlstate(exc: DBAPIError) -> str | None:
    original = getattr(exc, "orig", None)
    return getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)


async def _exclusively[T](
    sessions: async_sessionmaker,
    data_type: str,
    work: Callable[[AsyncSession], Awaitable[T]],
    statement_timeout: str,
    notice: Notice | None = None,
    *,
    dry_run: bool = False,
) -> T:
    """Run ``work`` in one transaction that holds ``data_type``'s exclusive lock.

    The lock is tried for without queueing for at most ``LOCK_TIMEOUT`` (writers never wait
    behind the request), then waited for in the queue at most ``QUEUE_TIMEOUT``; once held, any
    row lock is waited for at most ``ROW_LOCK_TIMEOUT``; then the transaction is rolled back
    and retried after a pause (``notice`` is told). A dry run writes nothing and takes no
    lock: it reads what is committed."""
    if dry_run:
        async with sessions.begin() as session:
            await _prepare(session, statement_timeout)
            return await work(session)
    for attempt in range(LOCK_ATTEMPTS):
        try:
            async with sessions.begin() as session:
                await lock_data_type(session, data_type, exclusive=True, wait=LOCK_TIMEOUT)
                await _prepare(session, statement_timeout)
                return await work(session)
        except (DBAPIError, LockBusy) as exc:
            if isinstance(exc, DBAPIError) and _sqlstate(exc) != _LOCK_NOT_AVAILABLE:
                raise
            delay = min(60, 2 ** (attempt + 1))
            event(
                logger,
                "IDENTITY_RECONCILE_LOCK_BUSY",
                data_type=data_type,
                attempt=attempt + 1,
                retry_in_seconds=delay,
            )
            if notice is not None:
                notice(
                    f"{data_type}: another transaction holds the identity lock or a row this "
                    f"batch needs; nothing was written, retrying in {delay} s "
                    f"(attempt {attempt + 1} of {LOCK_ATTEMPTS})"
                )
            await _pause(delay)
    raise LockUnavailable(
        f"{data_type}: could not get the identity lock (or a row it needs) in "
        f"{LOCK_ATTEMPTS} attempts (a long sync, deletion check or import of this data type "
        "held it); nothing of this batch was written. Run again later: an incremental run (or "
        "the next night's) settles everything since its checkpoint again and any re-settlement "
        "a run or decision still owes (identity_reconcile_state.pending_settle); a full run "
        "starts its passes over (--after skips the second pass up to a start time)"
    )


async def backfill_data_type(
    sessions: async_sessionmaker,
    data_type: str,
    *,
    batch_size: int = 5_000,
    dry_run: bool = False,
    after: datetime | None = None,
    statement_timeout: str = STATEMENT_TIMEOUT,
    progress: Callable[[str, BackfillStats, datetime], None] | None = None,
    notice: Notice | None = None,
) -> BackfillStats:
    """A full run for one data type (the whole archive; ``after`` skips the second pass
    up to a start time)."""
    spec = spec_for(data_type)
    total = BackfillStats(mode="full")
    async with sessions() as session:
        started = await source_chains.database_now(session)
    timeline = await _timeline(sessions, spec, batch_size, statement_timeout)
    planned, _ = await _settle_chains(
        sessions,
        spec,
        timeline,
        total,
        dry_run=dry_run,
        statement_timeout=statement_timeout,
        notice=notice,
    )
    await _link_pass(
        sessions,
        spec,
        planned,
        total,
        batch_size=batch_size,
        dry_run=dry_run,
        after=after,
        statement_timeout=statement_timeout,
        progress=progress,
        notice=notice,
    )
    stale = sorted(timeline.stale)
    total.start_mismatches = len(stale)
    for offset in range(0, len(stale), batch_size):
        chunk = stale[offset : offset + batch_size]
        stats = BackfillStats(mode="full")

        async def settle(session: AsyncSession, chunk=chunk, stats=stats) -> None:
            await _settle_starts(
                session,
                spec,
                [start for start, _ in chunk],
                [raw_id for _, raw_id in chunk],
                planned,
                stats,
                dry_run=dry_run,
                count=False,
            )

        await _exclusively(sessions, data_type, settle, statement_timeout, notice, dry_run=dry_run)
        total.add(stats)
    async with sessions() as session:
        undated = await session.scalar(
            select(func.count()).where(
                RawHealthData.data_type == data_type, RawHealthData.source_start_time.is_(None)
            )
        )
    total.unidentified += undated or 0
    if dry_run:
        summary = await _agreement_of(sessions, data_type)
    else:
        # Pass 2 counted every key of the pairs with evidence when it began (unless --after
        # skipped some), and settled every row (so no re-settlement is owed).
        summary = await _mark_counted(
            sessions, data_type, set(planned.evidence) if after is None else set(), recount=True
        )
        if after is None:
            await _settled(sessions, data_type)
    _review_agreeing(planned, total, summary)
    if not dry_run:
        await _save_state(sessions, data_type, started=started, stats=total)
    return total


async def relink_data_type(
    sessions: async_sessionmaker,
    data_type: str,
    apply: Callable[[AsyncSession, Chains], Awaitable[None]],
    *,
    batch_size: int = 5_000,
    dry_run: bool = False,
    statement_timeout: str = STATEMENT_TIMEOUT,
    progress: Callable[[str, BackfillStats, datetime], None] | None = None,
    notice: Notice | None = None,
) -> BackfillStats:
    """Apply an owner decision (``apply``, run under the exclusive lock first), decide the
    chains again from the sightings, then settle again exactly the rows whose devices changed:
    the rows of every link between two fingerprints that are no longer one device (each side
    then counts once), and, for two fingerprints that newly are, every row written since the
    later of them was first seen (``_settle_changed``)."""
    spec = spec_for(data_type)
    total = BackfillStats(mode="decision")
    async with sessions() as session:
        started = await source_chains.database_now(session)
    planned, change = await _settle_chains(
        sessions,
        spec,
        None,
        total,
        dry_run=dry_run,
        statement_timeout=statement_timeout,
        notice=notice,
        apply=apply,
    )
    await _settle_changed(
        sessions,
        spec,
        planned,
        change,
        total,
        since=None,
        batch_size=batch_size,
        dry_run=dry_run,
        statement_timeout=statement_timeout,
        progress=progress,
        notice=notice,
    )
    if not dry_run:
        await _settled(sessions, data_type)
    _review_agreeing(planned, total, await _agreement_of(sessions, data_type))
    if not dry_run:
        await _save_state(sessions, data_type, started=started, stats=total, checkpoint=False)
    return total


async def reconcile_data_type(
    sessions: async_sessionmaker,
    data_type: str,
    *,
    batch_size: int = 5_000,
    dry_run: bool = False,
    statement_timeout: str = STATEMENT_TIMEOUT,
    progress: Callable[[str, BackfillStats, datetime], None] | None = None,
    notice: Notice | None = None,
) -> BackfillStats:
    """An incremental run for one data type: chains from the sightings as they are, links
    for the rows changed since the checkpoint (minus ``OVERLAP``) and for every row whose
    device changed (``_settle_changed``). A data type that never had a full run gets its first
    full pass instead (``skipped`` says so), so none is left out for good."""
    spec = spec_for(data_type)
    state = await _reconcile_state(sessions, data_type)
    if state is None or state.full_run_at is None or state.checkpoint_at is None:
        total = await backfill_data_type(
            sessions,
            data_type,
            batch_size=batch_size,
            dry_run=dry_run,
            statement_timeout=statement_timeout,
            progress=progress,
            notice=notice,
        )
        total.skipped = "no full identity-backfill of this data type yet: ran its first full pass"
        return total
    total = BackfillStats(mode="incremental")
    async with sessions() as session:
        started = await source_chains.database_now(session)
    if not dry_run:
        await _count_shared(
            sessions, spec, batch_size=batch_size, statement_timeout=statement_timeout
        )
    planned, change = await _settle_chains(
        sessions,
        spec,
        None,
        total,
        dry_run=dry_run,
        statement_timeout=statement_timeout,
        notice=notice,
    )
    await _settle_changed(
        sessions,
        spec,
        planned,
        change,
        total,
        since=as_utc(state.checkpoint_at) - OVERLAP,
        batch_size=batch_size,
        dry_run=dry_run,
        statement_timeout=statement_timeout,
        progress=progress,
        notice=notice,
    )
    if not dry_run:
        await _settled(sessions, data_type)
    _review_agreeing(planned, total, await _agreement_of(sessions, data_type))
    if not dry_run:
        await _save_state(sessions, data_type, started=started, stats=total)
    return total


async def _settle_changed(
    sessions: async_sessionmaker,
    spec: DataTypeSpec,
    planned: Chains,
    change: DeviceChange,
    total: BackfillStats,
    *,
    since: datetime | None,
    batch_size: int,
    dry_run: bool,
    statement_timeout: str,
    progress: Callable[[str, BackfillStats, datetime], None] | None,
    notice: Notice | None,
) -> None:
    """Settle again, batch by batch, the start times of

    * rows created or given a new payload since ``since`` (an incremental run), and rows whose
      deletion mark was cleared since then;
    * every row of a link between two fingerprints that are no longer one device (``lapsed``:
      a rejected or refuted chain, a ``distinct`` decision), whatever its age - its two sides
      are linked again within each device, so neither counts twice;
    * every row first seen since the later of two fingerprints that are newly one device was
      (``joined``): each of its rows was, also one stored before migration 20261020_0011
      (without ``payload_received_at``).

    The start times are read first, each set in one short transaction (none is held open
    while they are settled), and kept as integers."""
    touched: datetime | None = None
    for left, right in change.joined:
        spans = [planned.spans.get(left), planned.spans.get(right)]
        if spans[0] is None or spans[1] is None:
            touched = datetime(1970, 1, 1, tzinfo=UTC)
            continue
        later = max(span.first for span in spans if span is not None) - OVERLAP
        touched = later if touched is None else min(touched, later)
    starts = await _collect_starts(
        sessions, spec.name, since=since, touched=touched, lapsed=change.lapsed
    )
    step = max(1, batch_size // 10)
    for offset in range(0, len(starts), step):
        chunk = [_from_micros(value) for value in starts[offset : offset + step]]
        stats = BackfillStats(mode=total.mode)

        async def settle(session: AsyncSession, chunk=chunk, stats=stats) -> None:
            await _settle_starts(
                session, spec, chunk, (), planned, stats, dry_run=dry_run, count=True
            )

        await _exclusively(sessions, spec.name, settle, statement_timeout, notice, dry_run=dry_run)
        total.add(stats)
        if progress is not None:
            progress(spec.name, total, chunk[-1])


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
    incremental: bool = False,
    statement_timeout: str = STATEMENT_TIMEOUT,
    progress: Callable[[str, BackfillStats, datetime], None] | None = None,
    notice: Notice | None = None,
) -> dict[str, BackfillStats]:
    """Raises ``BackfillRunning`` when another run holds the single-run lock.

    ``incremental``: only what changed since each data type's checkpoint (the nightly run);
    a data type that never had a full run gets its first full pass (``skipped`` says so).
    A data type that fails (its lock stays busy, a statement times out) is reported with its
    ``error`` and saved in ``identity_reconcile_state``; the run goes on with the others."""
    async with backfill_lock(sessions, dry_run=dry_run):
        names = list(data_types) if data_types else await data_types_in_archive(sessions)
        results: dict[str, BackfillStats] = {}
        for name in names:
            async with sessions() as session:
                started = await source_chains.database_now(session)
            try:
                if incremental:
                    results[name] = await reconcile_data_type(
                        sessions,
                        name,
                        batch_size=batch_size,
                        dry_run=dry_run,
                        statement_timeout=statement_timeout,
                        progress=progress,
                        notice=notice,
                    )
                else:
                    results[name] = await backfill_data_type(
                        sessions,
                        name,
                        batch_size=batch_size,
                        dry_run=dry_run,
                        after=after,
                        statement_timeout=statement_timeout,
                        progress=progress,
                        notice=notice,
                    )
            except Exception as exc:
                logger.exception(
                    "Identity reconciliation of a data type failed",
                    extra={"event": "IDENTITY_RECONCILE_DATA_TYPE_FAILED", "data_type": name},
                )
                if not dry_run:
                    await _save_state(
                        sessions,
                        name,
                        started=started,
                        stats=None,
                        error=f"{exc}",
                        mode="incremental" if incremental else "full",
                    )
                results[name] = BackfillStats(
                    mode="incremental" if incremental else "full", error=f"{exc}"[:2000]
                )
        if not dry_run:
            await analyze_identity_tables(sessions)
    return results


async def analyze_identity_tables(sessions: async_sessionmaker) -> None:
    async with sessions.begin() as session:
        if session.get_bind().dialect.name == "postgresql":
            # Fresh statistics for the lookups every stored page makes from now on.
            await session.execute(
                text(
                    "ANALYZE raw_health_data_identity, raw_health_data_supersessions, "
                    "raw_source_chains, raw_source_sightings, raw_source_devices"
                )
            )


def describe_source(chains: Chains, fingerprint: str) -> dict:
    return describe(chains, fingerprint)
