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
import re
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.logging import event
from app.core.timeutil import as_utc
from app.models import identity as identity_guards
from app.models import raw_versions as version_guards
from app.models.raw_versions import (
    FUNCTION_BODIES,
    HISTORY_VIEW,
    RENUMBER_SETTING,
    RawHealthDataEvent,
    RawHealthDataVersion,
)
from app.models.state import RawHealthData, RawJSON
from app.parsers.common import payload_hash
from app.services.storage import dialect_insert

logger = logging.getLogger(__name__)

# Every protective trigger: those of migration 20261020_0011 and of 20261021_0012.
PG_TRIGGER_SQL = {**version_guards.PG_TRIGGER_SQL, **identity_guards.PG_TRIGGER_SQL}
PROTECTIVE_TRIGGERS = (*version_guards.PROTECTIVE_TRIGGERS, *identity_guards.PROTECTIVE_TRIGGERS)
SQLITE_TRIGGER_SQL = {**version_guards.SQLITE_TRIGGER_SQL, **identity_guards.SQLITE_TRIGGER_SQL}

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
    Column("response_id", Integer),
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
            "response_id": row["response_id"],
        }
        if row["source_deleted_at"] is not None:
            version["source_deleted_at"] = _iso(row["source_deleted_at"])
        if include_payload:
            version["payload"] = row["raw_json"]
        record["versions"].append(version)
    events = await session.execute(
        select(
            RawHealthDataEvent.google_data_point_id,
            RawHealthDataEvent.event,
            RawHealthDataEvent.occurred_at,
            RawHealthDataEvent.previous_deleted_at,
        )
        .where(
            RawHealthDataEvent.data_type == data_type,
            RawHealthDataEvent.google_data_point_id.in_(keys),
        )
        .order_by(RawHealthDataEvent.id)
    )
    for google_id, kind, occurred, previous in events.tuples():
        if google_id in records:
            item = {"event": kind, "at": _iso(occurred)}
            if previous is not None:
                item["previous_deleted_at"] = _iso(previous)
            records[google_id].setdefault("events", []).append(item)
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
# last one and the current row included) never carry the same payload and stored text.
_REPEAT_SQL = text(
    "SELECT raw_id, version FROM ("
    "  SELECT raw_id, version, payload_hash, body,"
    "         lag(payload_hash) OVER (PARTITION BY raw_id ORDER BY version) AS previous,"
    "         lag(body) OVER (PARTITION BY raw_id ORDER BY version) AS previous_body"
    "  FROM ("
    "    SELECT raw_id, version, payload_hash, CAST(raw_json AS TEXT) AS body"
    "    FROM raw_health_data_versions"
    "    UNION ALL"
    "    SELECT r.id, 2147483647, r.payload_hash, CAST(r.raw_json AS TEXT) FROM raw_health_data r"
    "    WHERE r.id IN (SELECT raw_id FROM raw_health_data_versions)"
    "  ) AS chain"
    ") AS neighbours WHERE payload_hash = previous AND body = previous_body"
)

_PG_TRIGGERS_SQL = text(
    "SELECT c.relname, t.tgname, t.tgenabled, p.proname, p.prosrc, pg_get_triggerdef(t.oid) "
    "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_proc p ON p.oid = t.tgfoid "
    "WHERE NOT t.tgisinternal AND n.nspname = current_schema()"
)
_STAND_IN_TRIGGERS_SQL = text(
    "SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid) "
    "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
    "WHERE NOT t.tgisinternal AND c.relnamespace = pg_my_temp_schema()"
)
_ENABLED = {"O": "enabled (not ALWAYS)", "D": "disabled", "R": "replica-only", "A": "always"}
_STAND_IN = "healthos_verify_"
# pg_get_triggerdef names the table schema-qualified right after the events.
_RELATION = re.compile(r" ON \S+ ")


def _canonical(definition: str, table: str) -> str:
    return " ".join(_RELATION.sub(f" ON {table} ", definition, count=1).split())


async def _expected_pg_definitions(session: AsyncSession) -> dict[tuple[str, str], str]:
    """What this server makes of each expected ``CREATE TRIGGER``, as pg_get_triggerdef.

    The statements are run against empty temporary copies of the guarded tables inside a
    savepoint that is rolled back, so the comparison uses the server's own deparsing (no
    hand-written expectation that a PostgreSQL release could spell differently).
    """
    expected: dict[tuple[str, str], str] = {}
    tables = sorted({table for table, _ in PG_TRIGGER_SQL.values()})
    savepoint = await session.begin_nested()
    try:
        for table in tables:
            await session.execute(text(f"CREATE TEMP TABLE {_STAND_IN}{table} (LIKE {table})"))
        for table, sql in PG_TRIGGER_SQL.values():
            stand_in = sql.replace(f" ON {table} ", f" ON pg_temp.{_STAND_IN}{table} ", 1)
            await session.execute(text(stand_in))
        for relname, trigger, definition in (
            await session.execute(_STAND_IN_TRIGGERS_SQL)
        ).tuples():
            table = relname.removeprefix(_STAND_IN)
            expected[(table, trigger)] = _canonical(definition, table)
    finally:
        await savepoint.rollback()
    return expected


async def verify_guards(session: AsyncSession) -> list[str]:
    """Every protective trigger exists, always fires, and is exactly the one created.

    A trigger that is missing, disabled (``ALTER TABLE ... DISABLE TRIGGER``), demoted to
    ordinary ``ENABLE`` (then ``session_replication_role = replica`` skips it), recreated
    with other events, timing or a narrower ``WHEN`` (``WHEN (false)``), or whose function
    was replaced would let a delete or overwrite through unnoticed. Any other trigger on a
    guarded table is reported too.
    """
    errors: list[str] = []
    if session.get_bind().dialect.name != "postgresql":
        stored = {
            (table, trigger): sql
            for table, trigger, sql in (
                await session.execute(
                    text("SELECT tbl_name, name, sql FROM sqlite_master WHERE type = 'trigger'")
                )
            ).tuples()
        }
        for trigger, (table, sql) in SQLITE_TRIGGER_SQL.items():
            if (table, trigger) not in stored:
                errors.append(f"protective trigger missing: {table}.{trigger}")
            elif " ".join(stored[(table, trigger)].split()) != " ".join(sql.split()):
                errors.append(f"protective trigger {table}.{trigger} was redefined")
        return errors
    expected = await _expected_pg_definitions(session)
    found = {
        (table, trigger): (enabled, function, body, definition)
        for table, trigger, enabled, function, body, definition in (
            await session.execute(_PG_TRIGGERS_SQL)
        ).tuples()
    }
    guarded = {table for table, _, _ in PROTECTIVE_TRIGGERS}
    for table, trigger, function in PROTECTIVE_TRIGGERS:
        if (table, trigger) not in found:
            errors.append(f"protective trigger missing: {table}.{trigger}")
            continue
        enabled, actual_function, body, definition = found[(table, trigger)]
        if isinstance(enabled, bytes):
            enabled = enabled.decode()
        if enabled != "A":
            errors.append(
                f"protective trigger {table}.{trigger} is {_ENABLED.get(enabled, enabled)}; "
                f"run ALTER TABLE {table} ENABLE ALWAYS TRIGGER {trigger}"
            )
        if _canonical(definition, table) != expected.get((table, trigger)):
            errors.append(
                f"protective trigger {table}.{trigger} was redefined: {definition}; "
                f"expected {expected.get((table, trigger))}"
            )
        if actual_function != function:
            errors.append(f"protective trigger {table}.{trigger} calls {actual_function}()")
        elif body != FUNCTION_BODIES[function]:
            errors.append(f"protective function {function}() was changed")
    known = {(table, trigger) for table, trigger, _ in PROTECTIVE_TRIGGERS}
    for (table, trigger), (_, _, _, definition) in sorted(found.items()):
        if table in guarded and (table, trigger) not in known:
            errors.append(f"unexpected trigger on guarded table {table}: {definition}")
    return errors


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
    # Records in the dump that the live archive did not have at all: imported as rows of
    # their own, marked source-deleted (per data type).
    restored_records: dict[str, int] = field(default_factory=dict)
    source_hash_mismatches: list[str] = field(default_factory=list)
    # Payloads jsonb cannot store unchanged (e.g. 1e+16, \u0000): skipped, keep the dump.
    unrepresentable: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_rows": self.source_rows,
            "already_present": self.already_present,
            "imported": self.imported,
            "renumbered": self.renumbered,
            "records_with_new_versions": len(self.records),
            "restored_records": dict(sorted(self.restored_records.items())),
            "source_hash_mismatches": self.source_hash_mismatches,
            "unrepresentable": self.unrepresentable,
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
    first_seen_at: datetime | None = None
    source_deleted_at: datetime | None = None
    is_quarantined: bool | None = None
    quarantine_reason: str | None = None
    value: Any = None

    @property
    def label(self) -> str:
        return f"{self.data_type}/{self.google_id}"


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


async def newest_sighting(engine: AsyncEngine) -> datetime | None:
    """When the dump last saw any record (orders several dumps newest first)."""
    async with engine.connect() as connection:
        value = await connection.scalar(
            text("SELECT max(last_seen_at) FROM raw_health_data").columns(
                max=DateTime(timezone=True)
            )
        )
    return as_utc(value) if isinstance(value, datetime) else None


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
    "first_seen_at": DateTime(timezone=True),
    "source_deleted_at": DateTime(timezone=True),
}


def _source_query(table: str, columns: set[str]):
    # raw_json is read as text and parsed here: a json-typed column (dumps before
    # 20261020_0010) and SQLite text then behave the same, and a JSON string payload is
    # not mistaken for already-parsed text.
    def pick(*names: str) -> str:
        return next((name for name in names if name in columns), "NULL")

    sql = (
        "SELECT data_type, google_data_point_id, CAST(raw_json AS TEXT) AS raw_text, "
        "payload_hash, source_start_time, source_end_time, source_timezone, "
        f"{pick('received_at', 'payload_received_at')} AS received_at, last_seen_at, "
        f"{pick('superseded_at')} AS superseded_at, {pick('first_seen_at')} AS first_seen_at, "
        f"{pick('source_deleted_at')} AS source_deleted_at, "
        f"{pick('is_quarantined')} AS is_quarantined, "
        f"{pick('quarantine_reason')} AS quarantine_reason "
        f"FROM {table} ORDER BY data_type, google_data_point_id, last_seen_at"
    )
    return text(sql).columns(
        data_type=String(),
        google_data_point_id=String(),
        raw_text=String(),
        payload_hash=String(),
        source_timezone=String(),
        is_quarantined=Boolean(),
        quarantine_reason=String(),
        **_TIMES,
    )


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


def _candidate(row) -> _Candidate:
    return _Candidate(
        row.data_type,
        row.google_data_point_id,
        row.raw_text,
        row.payload_hash,
        as_utc(row.source_start_time),
        as_utc(row.source_end_time),
        row.source_timezone,
        as_utc(row.received_at),
        as_utc(row.last_seen_at),
        as_utc(row.superseded_at),
        as_utc(row.first_seen_at),
        as_utc(row.source_deleted_at),
        row.is_quarantined,
        row.quarantine_reason,
    )


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


@dataclass
class _Old:
    version: int
    row_id: Any
    payload_hash: str
    last_seen_at: datetime


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


@dataclass
class _Current:
    raw_id: Any
    payload_hash: str
    last_seen_at: datetime
    payload_source: str | None


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


def _merge(
    existing: list[_Old], current_hash: str, incoming: list[_Candidate]
) -> tuple[list[tuple[str, Any]], int]:
    """One record's history timeline with the dump's sightings slotted in by time.

    Returns the history entries (``("old", _Old)`` or ``("new", _Candidate)``, oldest
    first, the current row excluded) and how many sightings were already present. Equal
    neighbouring payloads are one occurrence: a sighting next to a stored version with the
    same payload adds nothing, and consecutive sightings of one payload become one version.
    """
    incoming = sorted(incoming, key=lambda item: item.last_seen_at)
    merged: list[tuple[str, Any]] = []
    position = 0
    for old in existing:
        # On a tie the archived sighting (older evidence) goes first.
        while position < len(incoming) and incoming[position].last_seen_at <= old.last_seen_at:
            merged.append(("new", incoming[position]))
            position += 1
        merged.append(("old", old))
    merged.extend(("new", item) for item in incoming[position:])
    merged.append(("current", current_hash))

    def digest(entry: tuple[str, Any]) -> str:
        kind, item = entry
        return item if kind == "current" else item.payload_hash

    timeline: list[tuple[str, Any]] = []
    present = 0
    start = 0
    while start < len(merged):
        end = start + 1
        while end < len(merged) and digest(merged[end]) == digest(merged[start]):
            end += 1
        run = merged[start:end]
        stored = [entry for entry in run if entry[0] != "new"]
        sightings = [item for kind, item in run if kind == "new"]
        if stored:
            present += len(sightings)
            timeline.extend(entry for entry in stored if entry[0] == "old")
        else:
            first = sightings[0]
            first.last_seen_at = max(item.last_seen_at for item in sightings)
            known = [item.superseded_at for item in sightings if item.superseded_at is not None]
            first.superseded_at = max(known) if known else None
            present += len(sightings) - 1
            timeline.append(("new", first))
        start = end
    return timeline, present


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
