"""Lock, restore and write archive-import rows within the caller's transaction."""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Table, and_, bindparam, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import as_utc
from app.models.raw_versions import RENUMBER_SETTING, RawHealthDataVersion
from app.models.state import RawHealthData
from app.parsers.common import payload_hash
from app.services.raw_history_types import IMPORT_SOURCE, ImportStats, _Candidate, _Current, _Old
from app.services.storage import dialect_insert

_ROUND_TRIP_SQL = text(
    "SELECT u.ord, CAST(u.t AS jsonb)::text FROM unnest(CAST(:texts AS text[])) "
    "WITH ORDINALITY AS u(t, ord)"
)


async def _jsonb_rejects(session: AsyncSession, candidates: list[_Candidate]) -> set[int]:
    """Positions of candidates whose payload jsonb would refuse or read back changed.

    The text checked is what the insert sends (SQLAlchemy's json.dumps of the value). A
    batch the cast rejects (\\u0000, Infinity) is retried row by row to find the culprits.
    """

    async def round_trip(texts: list[str]) -> list[str]:
        rows = await session.execute(_ROUND_TRIP_SQL, {"texts": texts})
        return [stored for _, stored in sorted(rows.tuples().all())]

    def changed(candidate: _Candidate, stored: str) -> bool:
        return payload_hash(json.loads(stored)) != candidate.payload_hash

    texts = [json.dumps(candidate.value) for candidate in candidates]
    try:
        async with session.begin_nested():
            stored = await round_trip(texts)
        return {i for i, item in enumerate(candidates) if changed(item, stored[i])}
    except DBAPIError:
        rejected = set()
        for index, (candidate, raw) in enumerate(zip(candidates, texts, strict=True)):
            try:
                async with session.begin_nested():
                    (stored_one,) = await round_trip([raw])
            except DBAPIError:
                rejected.add(index)
                continue
            if changed(candidate, stored_one):
                rejected.add(index)
        return rejected


async def _lock_current(
    session: AsyncSession, by_key: dict[tuple[str, str], list[_Candidate]]
) -> dict[tuple[str, str], _Current]:
    # Lock the current rows (in key order, like store_records): the version trigger
    # computes the next version number under the same row lock, so live writes and this
    # renumbering never interleave.
    current: dict[tuple[str, str], _Current] = {}
    by_type: dict[str, list[str]] = defaultdict(list)
    for data_type, google_id in by_key:
        by_type[data_type].append(google_id)
    for data_type in sorted(by_type):
        google_ids = sorted(by_type[data_type])
        rows = await session.execute(
            select(
                RawHealthData.google_data_point_id,
                RawHealthData.id,
                RawHealthData.payload_hash,
                RawHealthData.last_seen_at,
                RawHealthData.payload_source,
            )
            .where(
                RawHealthData.data_type == data_type,
                RawHealthData.google_data_point_id.in_(google_ids),
            )
            .order_by(RawHealthData.google_data_point_id)
            .with_for_update()
        )
        for google_id, raw_id, digest, last_seen, origin in rows.tuples():
            current[(data_type, google_id)] = _Current(raw_id, digest, as_utc(last_seen), origin)
    return current


async def _restore_record(session: AsyncSession, latest: _Candidate) -> _Current | None:
    """Insert a record the live archive lacks as its own (source-deleted) current row.

    ``None`` when a live write created the same record first (nothing inserted).
    """
    table = cast(Table, RawHealthData.__table__)
    statement = (
        dialect_insert(session, table)
        .values(
            id=uuid.uuid4(),
            google_data_point_id=latest.google_id,
            data_type=latest.data_type,
            source_start_time=latest.source_start_time,
            source_end_time=latest.source_end_time,
            source_timezone=latest.source_timezone,
            raw_json=latest.value,
            payload_hash=latest.payload_hash,
            first_seen_at=latest.first_seen_at or latest.received_at or latest.last_seen_at,
            last_seen_at=latest.last_seen_at,
            # Absent from the live archive: kept, but not shown as current data.
            source_deleted_at=latest.source_deleted_at or datetime.now(UTC),
            is_quarantined=bool(latest.is_quarantined),
            quarantine_reason=latest.quarantine_reason,
            payload_received_at=latest.received_at,
            payload_source=IMPORT_SOURCE,
        )
        .on_conflict_do_nothing(index_elements=["data_type", "google_data_point_id"])
        .returning(table.c.id)
    )
    raw_id = await session.scalar(statement)
    if raw_id is None:
        return None
    stored = await session.scalar(select(RawHealthData.raw_json).where(RawHealthData.id == raw_id))
    if payload_hash(stored) != latest.payload_hash:
        raise RuntimeError(f"restored payload would not read back unchanged: {latest.label}")
    return _Current(raw_id, latest.payload_hash, latest.last_seen_at, IMPORT_SOURCE)


async def _promote(session: AsyncSession, raw_id: Any, newest: _Candidate) -> None:
    """Make a newer dump sighting the current payload of an import-restored record."""
    await session.execute(
        update(RawHealthData)
        .where(RawHealthData.id == raw_id)
        .values(
            raw_json=newest.value,
            payload_hash=newest.payload_hash,
            source_start_time=newest.source_start_time,
            source_end_time=newest.source_end_time,
            source_timezone=newest.source_timezone,
            last_seen_at=newest.last_seen_at,
            payload_received_at=newest.received_at,
            payload_source=IMPORT_SOURCE,
        )
        .execution_options(synchronize_session=False)
    )


async def _load_history(session: AsyncSession, raw_ids: list[Any]) -> dict[Any, list[_Old]]:
    history: dict[Any, list[_Old]] = defaultdict(list)
    raw_ids = [raw_id for raw_id in raw_ids if raw_id is not None]
    for offset in range(0, len(raw_ids), 5_000):
        rows = await session.execute(
            select(
                RawHealthDataVersion.raw_id,
                RawHealthDataVersion.id,
                RawHealthDataVersion.version,
                RawHealthDataVersion.payload_hash,
                RawHealthDataVersion.last_seen_at,
            ).where(RawHealthDataVersion.raw_id.in_(raw_ids[offset : offset + 5_000]))
        )
        for raw_id, row_id, version, digest, last_seen in rows.tuples():
            history[raw_id].append(_Old(version, row_id, digest, as_utc(last_seen)))
    for items in history.values():
        items.sort(key=lambda item: item.version)
    return history


async def _write_timelines(
    session: AsyncSession,
    plans: dict[Any, list[tuple[str, Any]]],
    stats: ImportStats,
    *,
    postgresql: bool,
) -> None:
    moves: list[dict[str, Any]] = []
    inserts: list[dict[str, Any]] = []
    for raw_id, timeline in plans.items():
        for number, (kind, item) in enumerate(timeline, start=1):
            if kind == "old":
                if item.version != number:
                    moves.append({"row_id": item.row_id, "parked": -number})
                continue
            inserts.append(
                {
                    "raw_id": raw_id,
                    "data_type": item.data_type,
                    "google_data_point_id": item.google_id,
                    "version": number,
                    "raw_json": item.value,
                    "payload_hash": item.payload_hash,
                    "source_start_time": item.source_start_time,
                    "source_end_time": item.source_end_time,
                    "source_timezone": item.source_timezone,
                    "received_at": item.received_at,
                    "last_seen_at": item.last_seen_at,
                    # Replaced after it was last seen (exact when the dump knew it).
                    "superseded_at": item.superseded_at or item.last_seen_at,
                    "source": IMPORT_SOURCE,
                }
            )
    table = cast(Table, RawHealthDataVersion.__table__)
    if moves:
        if postgresql:
            # The history guard lets `version` change only inside this transaction.
            await session.execute(
                text("SELECT set_config(:name, '1', true)"), {"name": RENUMBER_SETTING}
            )
        # Park every number that changes below zero first, insert the new versions into
        # the freed numbers, then flip the parked ones back: the unique (raw_id, version)
        # constraint holds after every statement.
        await session.execute(
            update(table)
            .where(table.c.id == bindparam("row_id"))
            .values(version=bindparam("parked")),
            moves,
        )
        stats.renumbered += len(moves)
    await session.execute(table.insert(), inserts)
    if moves:
        await session.execute(
            update(table)
            .where(and_(table.c.raw_id.in_(list(plans)), table.c.version < 0))
            .values(version=-table.c.version)
        )
    # What the database stores must hash like the dump did (checked before, so this only
    # guards against a jsonb surprise; the chunk then rolls back as a whole).
    rows = await session.execute(
        select(
            table.c.raw_json, table.c.payload_hash, table.c.data_type, table.c.google_data_point_id
        ).where(table.c.raw_id.in_(list(plans)), table.c.source == IMPORT_SOURCE)
    )
    for raw_json, digest, data_type, google_id in rows.tuples():
        if payload_hash(raw_json) != digest:
            raise RuntimeError(
                f"imported payload would not read back unchanged: {data_type}/{google_id}"
            )
