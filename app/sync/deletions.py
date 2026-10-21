"""Mark archived records that a complete re-fetch of their window no longer returns.

Google Health has no tombstones in list responses: a deleted sleep or exercise record
simply stops appearing. After a window has been fetched from its first page to its last,
any archived record that the same filter must have matched, but which was not seen during
that fetch, is marked with ``source_deleted_at``. Nothing is deleted or overwritten; if
the record comes back, ``store_records`` clears the mark again.

"Not seen" is decided per record identity, not per row (app.services.identity.
present_records). A row is not marked while, in the same fetch, a row with its v2 key *and the
same reading* was seen

* that it is linked with (the row holding its record, or another duplicate of it), or of the
  *same device* - the same source fingerprint, or one joined to it by a usable chain
  (app/services/source_chains.py): the record under another id; or
* of a fingerprint not *established distinct* from the row's (never returned with another
  value for one key): possibly the same device renamed, which only the reconciler or the
  owner decides. The mark is deferred, never guessed.

A row of a fingerprint established distinct never keeps a candidate: of two devices that
recorded at one instant, one can be deleted alone. Nor does a row of the device with another
value: the candidate then holds a value Google no longer returns.

Superseded rows are candidates too: their holder stands for them while it is live, but a row
that counts again because its holder was marked (or would) must be marked when its record is
gone. A superseded row whose holder this fetch saw, unchanged since they were linked with the
same reading, is the holder's record and is not read again (that would re-read the raw JSON of
every superseded row on every complete fetch). Candidates are read, decided and marked in
chunks, so a long window never sits in memory at once; a row another transaction has locked
(a page writing it: it is being seen) is skipped rather than waited for, so this check never
waits for a writer, and a writer never waits for it in a circle.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, aliased

from app.core.logging import event
from app.google_health.data_types import (
    CIVIL_AFTER,
    CIVIL_BEFORE,
    AccessMethod,
    DataTypeSpec,
    RecordKind,
    query_time,
)
from app.models.identity import RawHealthDataSupersession, in_force
from app.models.state import RawHealthData
from app.services.identity import among, present_records
from app.services.source_chains import load_chains, lock_data_type

logger = logging.getLogger(__name__)

# Candidates read and decided at a time.
CHUNK = 2000


async def _note_deferred(
    session: AsyncSession,
    data_type: str,
    chains,
    pairs: Counter,
    window: tuple,
    now: datetime,
) -> None:
    """Keep what this complete fetch deferred (``identity_reconcile_state``, shown by
    ``health-mirror status`` and every reconciler run) until a later complete fetch of a
    window that covers it defers nothing, or defers something else."""
    from app.models.identity import IdentityReconcileState
    from app.services.identity import deferred_summary, identity_tables_missing

    if await identity_tables_missing(session):
        return
    state = await session.get(IdentityReconcileState, data_type)
    previous = json.loads(state.deferred_deletions) if state and state.deferred_deletions else None
    lower, upper = (
        value.isoformat() if hasattr(value, "isoformat") else str(value) for value in window
    )
    if not pairs:
        if previous is None or not (
            lower <= previous["window"][0] and previous["window"][1] <= upper
        ):
            return
        value = None
    else:
        value = json.dumps(
            {
                "at": now.isoformat(),
                "window": [lower, upper],
                "deferred": sum(pairs.values()),
                "sources": [
                    {**entry, "at": now.isoformat()} for entry in deferred_summary(chains, pairs)
                ],
            },
            sort_keys=True,
        )
    if state is None:
        state = IdentityReconcileState(data_type=data_type, runs=0)
        session.add(state)
    state.deferred_deletions = value
    await session.flush()


def deletion_scope(
    spec: DataTypeSpec, start: datetime, end: datetime
) -> tuple[InstrumentedAttribute, datetime, datetime] | None:
    """Return (stored column, lower, upper) that the list filter for the window covers.

    Mirrors ``DataTypeSpec.filter_for`` on the bounds the client actually sends
    (``query_time``: UTC, whole seconds), never on finer caller datetimes. ``None``
    means deletions cannot be inferred: unfiltered catalogs, and rollup/reconcile
    responses, which are derived aggregates rather than individually deletable source
    records.
    """
    if not spec.time_filtered or spec.access_method != AccessMethod.list:
        return None
    start, end = query_time(start), query_time(end)
    if spec.kind == RecordKind.daily:
        start_date = date.fromisoformat(start.isoformat()[:10])
        end_date = date.fromisoformat(end.isoformat()[:10])
        if end_date <= start_date or end.isoformat()[11:19] != "00:00:00":
            end_date += timedelta(days=1)
        lower = datetime.combine(start_date, time.min, UTC)
        upper = datetime.combine(end_date, time.min, UTC)
        return RawHealthData.source_start_time, lower, upper
    if spec.name == "sleep":
        return RawHealthData.source_end_time, start, end
    if spec.kind == RecordKind.session and spec.name != "electrocardiogram":
        # Civil start times: only records this far inside are certain to have matched.
        lower, upper = start + CIVIL_BEFORE, end - CIVIL_AFTER
        if lower >= upper:
            return None
        return RawHealthData.source_start_time, lower, upper
    # Samples, intervals, and electrocardiograms (whose filter has no upper bound,
    # so the returned set is a superset of this window).
    return RawHealthData.source_start_time, start, end


async def mark_source_deletions(
    session: AsyncSession,
    spec: DataTypeSpec,
    start: datetime,
    end: datetime,
    fetched_since: datetime,
) -> int:
    """Mark records in the window that were not seen since ``fetched_since``.

    ``store_records`` bumps ``last_seen_at`` for every returned record (changed or not),
    so a record in scope that is older than the start of the complete fetch was not
    returned by Google.
    """
    scope = deletion_scope(spec, start, end)
    if scope is None:
        return 0
    column, lower, upper = scope
    await lock_data_type(session, spec.name, exclusive=False)
    unseen = (
        RawHealthData.data_type == spec.name,
        RawHealthData.source_deleted_at.is_(None),
        RawHealthData.last_seen_at < fetched_since,
        column >= lower,
        column < upper,
    )
    link, holder = aliased(RawHealthDataSupersession), aliased(RawHealthData)
    # Superseded by a link in force with the same reading, its holder seen by this fetch and
    # unchanged since: the holder's record, present.
    held = (
        select(link.old_raw_id)
        .join(holder, holder.id == link.new_raw_id)
        .where(
            link.old_raw_id == RawHealthData.id,
            link.same_reading.is_(True),
            holder.payload_hash == link.new_payload_hash,
            holder.last_seen_at >= fetched_since,
            holder.source_deleted_at.is_(None),
            in_force(link),
        )
        .exists()
    )
    marked = kept = deferred = 0
    pairs: Counter = Counter()
    now = datetime.now(UTC)
    cursor: tuple | None = None
    chains = await load_chains(session, spec.name)
    postgresql = session.get_bind().dialect.name == "postgresql"
    while True:
        query = select(
            RawHealthData.id,
            RawHealthData.raw_json,
            RawHealthData.source_start_time,
            column,
        ).where(*unseen, ~held)
        if cursor is not None:
            query = query.where(tuple_(column, RawHealthData.id) > cursor)
        chunk = (
            (await session.execute(query.order_by(column, RawHealthData.id).limit(CHUNK)))
            .tuples()
            .all()
        )
        if not chunk:
            break
        cursor = (chunk[-1][3], chunk[-1][0])
        presence = await present_records(
            session,
            spec,
            [(raw_id, raw_json, start) for raw_id, raw_json, start, _ in chunk],
            fetched_since,
            chains,
        )
        kept += len(presence.present)
        deferred += len(presence.deferred)
        pairs.update(presence.deferred_pairs)
        spared = presence.present | presence.deferred
        ids = [raw_id for raw_id, _, _, _ in chunk if raw_id not in spared]
        if ids and postgresql:
            # Rows a writer holds are being seen right now: skipped, never waited for.
            ids = list(
                (
                    await session.scalars(
                        select(RawHealthData.id)
                        .where(*unseen, among(session, RawHealthData.id, ids))
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
        if ids:
            result = await session.execute(
                update(RawHealthData)
                .where(*unseen, among(session, RawHealthData.id, ids))
                .values(source_deleted_at=now)
                .execution_options(synchronize_session=False)
            )
            # An UPDATE always yields a CursorResult; AsyncSession.execute is typed as Result.
            marked += cast(CursorResult[Any], result).rowcount or 0
        if len(chunk) < CHUNK:
            break
    if kept:
        event(logger, "SOURCE_DELETIONS_KEPT_BY_IDENTITY", data_type=spec.name, count=kept)
    if deferred:
        # Possibly renamed: the reconciler or the owner decides (identity-backfill, nightly;
        # identity-decide).
        event(logger, "SOURCE_DELETIONS_DEFERRED", data_type=spec.name, count=deferred)
    await _note_deferred(session, spec.name, chains, pairs, (lower, upper), now)
    if marked:
        event(logger, "SOURCE_DELETIONS_MARKED", data_type=spec.name, count=marked)
    return marked
