"""Verify raw version integrity and the archive's protective triggers."""

from __future__ import annotations

import re

from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.guards import (
    FUNCTION_BODIES,
    PG_TRIGGER_SQL,
    PROTECTIVE_TRIGGERS,
    SQLITE_TRIGGER_SQL,
)
from app.models.raw_versions import RawHealthDataVersion
from app.parsers.common import payload_hash

# PG_TRIGGER_SQL, PROTECTIVE_TRIGGERS, SQLITE_TRIGGER_SQL, FUNCTION_BODIES: every protective
# trigger through migration 20261025_0020, joined in app/models/guards.py.


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
    from app.models.retrieval import CHECKPOINT_TABLE, RECEIPT_TABLES

    missing = await session.run_sync(
        lambda sync: [
            name
            for name in (*RECEIPT_TABLES, CHECKPOINT_TABLE)
            if not inspect(sync.connection()).has_table(name)
        ]
    )
    if missing:
        return [
            "retrieval evidence schema 20261025_0015 missing: "
            + ", ".join(missing)
            + "; run `alembic upgrade head` before verifying"
        ]
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
    functions = {function for _, _, function in PROTECTIVE_TRIGGERS}
    for (table, trigger), (_, function, _, definition) in sorted(found.items()):
        if table in guarded and (table, trigger) not in known:
            errors.append(f"unexpected trigger on guarded table {table}: {definition}")
        elif table not in guarded and function in functions:
            # A guard on a table that is not guarded: a guarded table renamed away (and an
            # unguarded copy put in its place) keeps its triggers.
            errors.append(
                f"protective trigger {trigger} sits on {table}, which is not a guarded table "
                "(was a guarded table renamed or replaced?)"
            )
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
