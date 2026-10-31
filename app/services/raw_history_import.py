"""Coordinate source streaming and per-chunk archive-import transactions."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.logging import event
from app.parsers.common import payload_hash
from app.services.raw_history_source import _candidate, _source_columns, _source_query
from app.services.raw_history_timeline import _merge
from app.services.raw_history_types import IMPORT_SOURCE, ImportStats, _Candidate, _Current, _Old
from app.services.raw_history_writing import (
    _jsonb_rejects,
    _load_history,
    _lock_current,
    _promote,
    _restore_record,
    _write_timelines,
)

# Keep the existing event logger name when importing through the public facade.
logger = logging.getLogger("app.services.raw_history")


async def import_versions(
    target: async_sessionmaker,
    source_engine: AsyncEngine,
    *,
    batch_size: int = 2_000,
    dry_run: bool = False,
) -> ImportStats:
    """Insert every (record, payload) occurrence of the source dump that the archive lacks.

    Each record's versions and the dump's sightings are merged into one timeline by the
    time they were last seen; a dump payload is "already present" only when it lands next
    to the same payload in that timeline, so a payload that returned later (A, B, A) is
    kept as its own occurrence. Existing history rows keep their relative order and are
    only renumbered; current rows are never modified. A record the live archive does not
    have at all is imported as a new row marked ``source_deleted_at`` (its latest dump
    payload as current version, earlier ones as history), so nothing that ever existed is
    lost. Payloads jsonb cannot store unchanged are skipped and listed.
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
            pending: list[_Candidate] = []
            async for chunk in result.partitions(batch_size):
                pending.extend(_candidate(row) for row in chunk)
                if len(pending) < batch_size:
                    continue
                # Keep every record's rows in one chunk: cut before the last record.
                last = (pending[-1].data_type, pending[-1].google_id)
                cut = len(pending)
                while cut and (pending[cut - 1].data_type, pending[cut - 1].google_id) == last:
                    cut -= 1
                if cut:
                    await _import_chunk(target, pending[:cut], stats, dry_run=dry_run)
                    pending = pending[cut:]
            if pending:
                await _import_chunk(target, pending, stats, dry_run=dry_run)
    event(
        logger,
        "RAW_VERSIONS_IMPORTED",
        dry_run=dry_run,
        unrepresentable=len(stats.unrepresentable),
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
    stats.source_rows += len(candidates)
    usable: list[_Candidate] = []
    for candidate in candidates:
        try:
            candidate.value = json.loads(candidate.raw_text)
            intact = payload_hash(candidate.value) == candidate.payload_hash
        except ValueError:
            intact = False
        if intact:
            usable.append(candidate)
        else:
            stats.source_hash_mismatches.append(candidate.label)
    async with target.begin() as session:
        postgresql = session.get_bind().dialect.name == "postgresql"
        if postgresql and usable:
            rejected = await _jsonb_rejects(session, usable)
            stats.unrepresentable.extend(usable[i].label for i in sorted(rejected))
            usable = [item for i, item in enumerate(usable) if i not in rejected]
        by_key: dict[tuple[str, str], list[_Candidate]] = defaultdict(list)
        for candidate in usable:
            by_key[(candidate.data_type, candidate.google_id)].append(candidate)
        current = await _lock_current(session, by_key)
        missing = [key for key in by_key if key not in current]
        for key in missing:
            items = by_key[key]
            latest = max(items, key=lambda item: item.last_seen_at)
            if dry_run:
                restored: _Current | None = _Current(
                    None, latest.payload_hash, latest.last_seen_at, IMPORT_SOURCE
                )
            else:
                restored = await _restore_record(session, latest)
            if restored is None:
                # A live write created the record meanwhile: merge into that row instead.
                current.update(await _lock_current(session, {key: items}))
                continue
            items.remove(latest)
            current[key] = restored
            stats.restored_records[key[0]] = stats.restored_records.get(key[0], 0) + 1
        # A restored record (current row written by an import) may meet a newer sighting
        # in a later dump: that payload becomes current, the trigger keeps the old one.
        promoted: dict[Any, _Old] = {}
        for key, items in by_key.items():
            row = current[key]
            newer = [item for item in items if item.last_seen_at > row.last_seen_at]
            if row.payload_source != IMPORT_SOURCE or not newer:
                continue
            newest = max(newer, key=lambda item: item.last_seen_at)
            items.remove(newest)
            stats.already_present += newest.payload_hash == row.payload_hash
            stats.imported += newest.payload_hash != row.payload_hash
            if not dry_run:
                await _promote(session, row.raw_id, newest)
            elif newest.payload_hash != row.payload_hash:
                promoted[key] = _Old(1 << 30, None, row.payload_hash, row.last_seen_at)
            current[key] = _Current(
                row.raw_id, newest.payload_hash, newest.last_seen_at, IMPORT_SOURCE
            )
        history = await _load_history(session, [row.raw_id for row in current.values()])

        plans: dict[Any, list[tuple[str, Any]]] = {}
        for key, items in by_key.items():
            raw_id, current_hash = current[key].raw_id, current[key].payload_hash
            stored = history.get(raw_id, []) if raw_id is not None else []
            if key in promoted:
                stored = [*stored, promoted[key]]
            timeline, present = _merge(stored, current_hash, items)
            stats.already_present += present
            new = [item for kind, item in timeline if kind == "new"]
            if not new:
                continue
            stats.imported += len(new)
            stats.records.add(raw_id if raw_id is not None else key)
            plans[raw_id] = timeline
        if dry_run or not plans:
            return
        await _write_timelines(session, plans, stats, postgresql=postgresql)
