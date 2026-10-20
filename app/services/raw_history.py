"""Read, verify and back-fill the version history of raw records.

* :func:`record_history` lists every version of selected records (``health-mirror history``).
* :func:`verify_versions` checks history integrity (``health-mirror verify``).
* :func:`import_versions` copies payloads that only survive in an old archive dump into
  ``raw_health_data_versions`` (``health-mirror import-versions``).

See app/models/raw_versions.py for how versions are written.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    bindparam,
    func,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.logging import event
from app.core.timeutil import as_utc
from app.models.raw_versions import HISTORY_VIEW, RawHealthDataVersion
from app.models.state import RawHealthData, RawJSON
from app.parsers.common import payload_hash

logger = logging.getLogger(__name__)

IMPORT_SOURCE = "archive-import"

# The view from migration 20261020_0011, described for SQLAlchemy (not part of Base, so
# Alembic never tries to create it as a table).
history_view = Table(
    HISTORY_VIEW,
    MetaData(),
    Column("data_type", String(128)),
    Column("google_data_point_id", String(512)),
    Column("raw_id"),
    Column("version", Integer),
    Column("is_current", Boolean),
    Column("raw_json", RawJSON),
    Column("payload_hash", String(64)),
    Column("source", String(32)),
    Column("valid_from", DateTime(timezone=True)),
    Column("valid_to", DateTime(timezone=True)),
    Column("last_seen_at", DateTime(timezone=True)),
    Column("source_start_time", DateTime(timezone=True)),
    Column("source_end_time", DateTime(timezone=True)),
    Column("source_deleted_at", DateTime(timezone=True)),
)


def _iso(value: datetime | None) -> str | None:
    return as_utc(value).isoformat() if value is not None else None


async def record_history(
    session: AsyncSession,
    data_type: str,
    *,
    google_id: str | None = None,
    since: datetime | None = None,
    limit: int = 20,
    include_payload: bool = True,
) -> list[dict[str, Any]]:
    """Every version of the selected records, oldest first.

    With ``google_id``: that record (also when it has only one version). Otherwise the
    ``limit`` most recently changed records of ``data_type`` that have earlier versions,
    optionally only those replaced at or after ``since``.
    """
    if google_id is not None:
        keys = [google_id]
    else:
        versions = RawHealthDataVersion
        query = select(versions.google_data_point_id).where(versions.data_type == data_type)
        if since is not None:
            query = query.where(versions.superseded_at >= since)
        last_change = func.max(versions.superseded_at)
        query = (
            query.group_by(versions.google_data_point_id)
            .order_by(last_change.desc().nulls_last(), versions.google_data_point_id)
            .limit(limit)
        )
        keys = list((await session.scalars(query)).all())
    if not keys:
        return []
    rows = await session.execute(
        select(history_view)
        .where(
            history_view.c.data_type == data_type,
            history_view.c.google_data_point_id.in_(keys),
        )
        .order_by(history_view.c.google_data_point_id, history_view.c.version)
    )
    records: dict[str, dict[str, Any]] = {}
    for row in rows.mappings():
        record = records.setdefault(
            row["google_data_point_id"],
            {
                "data_type": row["data_type"],
                "google_data_point_id": row["google_data_point_id"],
                "versions": [],
            },
        )
        version = {
            "version": row["version"],
            "is_current": bool(row["is_current"]),
            "valid_from": _iso(row["valid_from"]),
            "valid_to": _iso(row["valid_to"]),
            "last_seen_at": _iso(row["last_seen_at"]),
            "source": row["source"],
            "payload_hash": row["payload_hash"],
        }
        if row["source_deleted_at"] is not None:
            version["source_deleted_at"] = _iso(row["source_deleted_at"])
        if include_payload:
            version["payload"] = row["raw_json"]
        record["versions"].append(version)
    return [records[key] for key in keys if key in records]


# --- integrity ---------------------------------------------------------------------------

_CONTIGUITY_SQL = text(
    "SELECT raw_id, count(*), min(version), max(version) FROM raw_health_data_versions "
    "GROUP BY raw_id HAVING min(version) <> 1 OR max(version) <> count(*)"
)
_KEY_MISMATCH_SQL = text(
    "SELECT v.id FROM raw_health_data_versions v JOIN raw_health_data r ON r.id = v.raw_id "
    "WHERE v.data_type <> r.data_type OR v.google_data_point_id <> r.google_data_point_id"
)
_ORPHAN_SQL = text(
    "SELECT v.id FROM raw_health_data_versions v LEFT JOIN raw_health_data r "
    "ON r.id = v.raw_id WHERE r.id IS NULL"
)
_DUPLICATE_CURRENT_SQL = text(
    "SELECT data_type, google_data_point_id FROM raw_health_data "
    "GROUP BY data_type, google_data_point_id HAVING count(*) > 1"
)
# A version exists only because the payload changed: two neighbouring versions (the
# last one and the current row included) never carry the same payload.
_REPEAT_SQL = text(
    "SELECT raw_id, version FROM ("
    "  SELECT raw_id, version, payload_hash,"
    "         lag(payload_hash) OVER (PARTITION BY raw_id ORDER BY version) AS previous"
    "  FROM ("
    "    SELECT raw_id, version, payload_hash FROM raw_health_data_versions"
    "    UNION ALL"
    "    SELECT r.id, 2147483647, r.payload_hash FROM raw_health_data r"
    "    WHERE r.id IN (SELECT raw_id FROM raw_health_data_versions)"
    "  ) AS chain"
    ") AS neighbours WHERE payload_hash = previous"
)


async def verify_versions(session: AsyncSession) -> tuple[int, list[str]]:
    """Check every history row; returns (rows checked, error messages)."""
    errors: list[str] = []
    count = 0
    versions = RawHealthDataVersion
    rows = await session.stream(
        select(
            versions.id,
            versions.data_type,
            versions.google_data_point_id,
            versions.version,
            versions.raw_json,
            versions.payload_hash,
        ).execution_options(yield_per=1_000)
    )
    async for row_id, data_type, google_id, version, raw_json, expected in rows:
        count += 1
        if payload_hash(raw_json) != expected:
            errors.append(
                f"version payload hash mismatch: {data_type}/{google_id} "
                f"version {version} (history id {row_id})"
            )
    for raw_id, total, low, high in (await session.execute(_CONTIGUITY_SQL)).tuples():
        errors.append(
            f"version numbers not contiguous for raw row {raw_id}: "
            f"{total} history row(s) numbered {low}..{high}"
        )
    for (row_id,) in (await session.execute(_KEY_MISMATCH_SQL)).tuples():
        errors.append(f"history id {row_id} does not match its record's key")
    for (row_id,) in (await session.execute(_ORPHAN_SQL)).tuples():
        errors.append(f"history id {row_id} has no current row")
    for data_type, google_id in (await session.execute(_DUPLICATE_CURRENT_SQL)).tuples():
        errors.append(f"more than one current version: {data_type}/{google_id}")
    for raw_id, version in (await session.execute(_REPEAT_SQL)).tuples():
        label = "current version" if version == 2147483647 else f"version {version}"
        errors.append(f"{label} of raw row {raw_id} repeats the previous payload")
    return count, errors


# --- archive import ----------------------------------------------------------------------


@dataclass
class ImportStats:
    source_rows: int = 0
    already_present: int = 0
    imported: int = 0
    renumbered: int = 0
    # Records with at least one imported version (raw ids, counted once across chunks).
    records: set[Any] = field(default_factory=set)
    # Records in the dump that the live archive does not have at all (reported, not copied).
    missing_records: dict[str, int] = field(default_factory=dict)
    source_hash_mismatches: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_rows": self.source_rows,
            "already_present": self.already_present,
            "imported": self.imported,
            "renumbered": self.renumbered,
            "records_with_new_versions": len(self.records),
            "missing_records": dict(sorted(self.missing_records.items())),
            "source_hash_mismatches": self.source_hash_mismatches,
        }


@dataclass
class _Candidate:
    data_type: str
    google_id: str
    raw_text: str
    payload_hash: str
    source_start_time: datetime | None
    source_end_time: datetime | None
    source_timezone: str | None
    received_at: datetime | None
    last_seen_at: datetime
    superseded_at: datetime | None


def source_database_url(from_db: str, target_url: str) -> str:
    """A database name on the target's server, or a full SQLAlchemy URL."""
    if "://" in from_db:
        url = make_url(from_db)
        if url.drivername == "postgresql":
            url = url.set(drivername="postgresql+asyncpg")
    else:
        url = make_url(target_url).set(database=from_db)
    if url.render_as_string(hide_password=False) == target_url:
        raise ValueError("the source database is the live archive itself")
    return url.render_as_string(hide_password=False)


async def _source_columns(engine: AsyncEngine) -> dict[str, set[str]]:
    def read(connection) -> dict[str, set[str]]:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        return {
            table: {column["name"] for column in inspector.get_columns(table)}
            for table in ("raw_health_data", RawHealthDataVersion.__tablename__)
            if table in tables
        }

    async with engine.connect() as connection:
        return await connection.run_sync(read)


_TIMES = {
    "source_start_time": DateTime(timezone=True),
    "source_end_time": DateTime(timezone=True),
    "received_at": DateTime(timezone=True),
    "last_seen_at": DateTime(timezone=True),
    "superseded_at": DateTime(timezone=True),
}


def _source_query(table: str, columns: set[str]):
    # raw_json is read as text and parsed here: a json-typed column (dumps before
    # 20261020_0010) and SQLite text then behave the same, and a JSON string payload is
    # not mistaken for already-parsed text.
    received = (
        "received_at"
        if "received_at" in columns
        else ("payload_received_at" if "payload_received_at" in columns else "NULL")
    )
    superseded = "superseded_at" if "superseded_at" in columns else "NULL"
    sql = (
        "SELECT data_type, google_data_point_id, CAST(raw_json AS TEXT) AS raw_text, "
        "payload_hash, source_start_time, source_end_time, source_timezone, "
        f"{received} AS received_at, last_seen_at, {superseded} AS superseded_at "
        f"FROM {table} ORDER BY data_type, google_data_point_id, last_seen_at"
    )
    return text(sql).columns(
        data_type=String(),
        google_data_point_id=String(),
        raw_text=String(),
        payload_hash=String(),
        source_timezone=String(),
        **_TIMES,
    )


async def import_versions(
    target: async_sessionmaker,
    source_engine: AsyncEngine,
    *,
    batch_size: int = 2_000,
    dry_run: bool = False,
) -> ImportStats:
    """Insert every (record, payload) of the source dump that the archive lacks.

    Payloads already present for a record (as its current row or any version) are
    skipped, so a second run adds nothing. New versions are slotted into each record's
    history by the time they were last seen; existing history rows keep their relative
    order and are only renumbered. Current rows are never modified.
    """
    stats = ImportStats()
    tables = await _source_columns(source_engine)
    if "raw_health_data" not in tables:
        raise ValueError("the source database has no raw_health_data table")
    for table, columns in tables.items():
        async with source_engine.connect() as connection:
            result = await connection.stream(
                _source_query(table, columns).execution_options(yield_per=batch_size)
            )
            async for chunk in result.partitions(batch_size):
                candidates = [
                    _Candidate(
                        row.data_type,
                        row.google_data_point_id,
                        row.raw_text,
                        row.payload_hash,
                        row.source_start_time,
                        row.source_end_time,
                        row.source_timezone,
                        row.received_at,
                        row.last_seen_at,
                        row.superseded_at,
                    )
                    for row in chunk
                ]
                stats.source_rows += len(candidates)
                await _import_chunk(target, candidates, stats, dry_run=dry_run)
    event(
        logger,
        "RAW_VERSIONS_IMPORTED",
        dry_run=dry_run,
        **{key: value for key, value in stats.as_dict().items() if isinstance(value, int)},
    )
    return stats


async def _import_chunk(
    target: async_sessionmaker,
    candidates: Sequence[_Candidate],
    stats: ImportStats,
    *,
    dry_run: bool,
) -> None:
    by_type: dict[str, list[_Candidate]] = defaultdict(list)
    for candidate in candidates:
        by_type[candidate.data_type].append(candidate)
    async with target.begin() as session:
        # Lock the current rows: the version trigger computes the next version number
        # under the same row lock, so live writes and this renumbering never interleave.
        current: dict[tuple[str, str], tuple[Any, str]] = {}
        for data_type, items in by_type.items():
            rows = await session.execute(
                select(
                    RawHealthData.id, RawHealthData.google_data_point_id, RawHealthData.payload_hash
                )
                .where(
                    RawHealthData.data_type == data_type,
                    RawHealthData.google_data_point_id.in_({item.google_id for item in items}),
                )
                .with_for_update()
            )
            for raw_id, google_id, digest in rows.tuples():
                current[(data_type, google_id)] = (raw_id, digest)
        history: dict[Any, list[tuple[int, int, str, datetime]]] = defaultdict(list)
        raw_ids = [raw_id for raw_id, _ in current.values()]
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
                history[raw_id].append((version, row_id, digest, as_utc(last_seen)))

        new: dict[Any, dict[str, _Candidate]] = defaultdict(dict)
        for candidate in candidates:
            key = (candidate.data_type, candidate.google_id)
            if key not in current:
                stats.missing_records[candidate.data_type] = (
                    stats.missing_records.get(candidate.data_type, 0) + 1
                )
                continue
            raw_id, current_hash = current[key]
            known = {current_hash, *(item[2] for item in history[raw_id])}
            if candidate.payload_hash in known:
                stats.already_present += 1
                continue
            try:
                intact = payload_hash(json.loads(candidate.raw_text)) == candidate.payload_hash
            except ValueError:
                intact = False
            if not intact:
                stats.source_hash_mismatches.append(f"{key[0]}/{key[1]}")
                continue
            seen = new[raw_id].get(candidate.payload_hash)
            if seen is None:
                new[raw_id][candidate.payload_hash] = candidate
                stats.imported += 1
            else:
                # The same payload in the dump's raw and history tables: keep the later sighting.
                seen.last_seen_at = max(as_utc(seen.last_seen_at), as_utc(candidate.last_seen_at))
        if not new:
            return
        stats.records.update(new)
        if dry_run:
            return
        await _insert_versions(session, new, history, stats)


async def _insert_versions(
    session: AsyncSession,
    new: dict[Any, dict[str, _Candidate]],
    history: dict[Any, list[tuple[int, int, str, datetime]]],
    stats: ImportStats,
) -> None:
    moves: list[dict[str, Any]] = []
    inserts: list[dict[str, Any]] = []
    for raw_id, by_hash in new.items():
        existing = sorted(history[raw_id])
        incoming = sorted(by_hash.values(), key=lambda item: as_utc(item.last_seen_at))
        # Merge by last-seen time; the existing history keeps its order, and on a tie the
        # archived payload (older evidence) goes first.
        merged: list[tuple[str, Any]] = []
        position = 0
        for version, row_id, _, last_seen in existing:
            while position < len(incoming) and as_utc(incoming[position].last_seen_at) <= last_seen:
                merged.append(("new", incoming[position]))
                position += 1
            merged.append(("old", (version, row_id)))
        merged.extend(("new", item) for item in incoming[position:])
        for number, (kind, item) in enumerate(merged, start=1):
            if kind == "old":
                version, row_id = item
                if version != number:
                    moves.append({"row_id": row_id, "parked": -number})
                continue
            inserts.append(
                {
                    "raw_id": raw_id,
                    "data_type": item.data_type,
                    "google_data_point_id": item.google_id,
                    "version": -number,
                    "raw_json": json.loads(item.raw_text),
                    "payload_hash": item.payload_hash,
                    "source_start_time": as_utc(item.source_start_time),
                    "source_end_time": as_utc(item.source_end_time),
                    "source_timezone": item.source_timezone,
                    "received_at": as_utc(item.received_at),
                    "last_seen_at": as_utc(item.last_seen_at),
                    "superseded_at": as_utc(item.superseded_at),
                    "source": IMPORT_SOURCE,
                }
            )
    table = cast(Table, RawHealthDataVersion.__table__)
    # Park every number that changes below zero first, then flip them back: the unique
    # (raw_id, version) constraint holds after every statement.
    if moves:
        await session.execute(
            update(table)
            .where(table.c.id == bindparam("row_id"))
            .values(version=bindparam("parked")),
            moves,
        )
        stats.renumbered += len(moves)
    await session.execute(table.insert(), inserts)
    await session.execute(
        update(table)
        .where(and_(table.c.raw_id.in_(list(new)), table.c.version < 0))
        .values(version=-table.c.version)
    )
    # What the database stores must hash like the dump did (jsonb may respell numbers).
    rows = await session.execute(
        select(
            table.c.raw_json, table.c.payload_hash, table.c.data_type, table.c.google_data_point_id
        ).where(table.c.raw_id.in_(list(new)), table.c.source == IMPORT_SOURCE)
    )
    for raw_json, digest, data_type, google_id in rows.tuples():
        if payload_hash(raw_json) != digest:
            raise RuntimeError(
                f"imported payload would not read back unchanged: {data_type}/{google_id}"
            )
