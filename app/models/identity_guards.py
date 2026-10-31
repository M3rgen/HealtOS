"""Append-only identity archive and widening-only sighting guards (migration 0012).

Importing this module installs the dialect-specific metadata listeners once. ORM tables
load raw_versions first: its DDL defines healthos_refuse_change(), used by these triggers.
The SQL is frozen in the migration and compared by tests/test_identity_keys.py.
"""

from sqlalchemy import DDL, event

from app.db.base import Base
from app.models import identity_tables  # noqa: F401 - register tables and raw_versions first
from app.models.identity_constants import IDENTITY_TABLES, SIGHTING_TABLE

SIGHTINGS_FUNCTION = "healthos_sightings_widen_only"
FUNCTION_BODIES = {
    SIGHTINGS_FUNCTION: f"""
BEGIN
    IF NEW.data_type IS DISTINCT FROM OLD.data_type
       OR NEW.fingerprint IS DISTINCT FROM OLD.fingerprint
       OR NEW.stable_class IS DISTINCT FROM OLD.stable_class
       OR NEW.label IS DISTINCT FROM OLD.label
       OR NEW.data_source IS DISTINCT FROM OLD.data_source
       OR NEW.first_seen_at > OLD.first_seen_at
       OR NEW.last_seen_at < OLD.last_seen_at
       OR NEW.newest_record_at < OLD.newest_record_at
       OR (OLD.newest_record_at IS NOT NULL AND NEW.newest_record_at IS NULL)
       OR (OLD.complete AND NOT NEW.complete) THEN
        RAISE EXCEPTION USING MESSAGE =
            '{SIGHTING_TABLE} only widens: ' || TG_OP || ' refused';
    END IF;
    RETURN NEW;
END
""",
}

# trigger -> (table, timing/events/level, function)
_PG_TRIGGERS = {}
for _table in IDENTITY_TABLES:
    _PG_TRIGGERS[f"{_table}_append_only"] = (
        _table,
        f"BEFORE UPDATE OR DELETE ON {_table} FOR EACH ROW",
        "healthos_refuse_change",
    )
    _PG_TRIGGERS[f"{_table}_no_truncate"] = (
        _table,
        f"BEFORE TRUNCATE ON {_table} FOR EACH STATEMENT",
        "healthos_refuse_change",
    )
_PG_TRIGGERS[f"{SIGHTING_TABLE}_widen_only"] = (
    SIGHTING_TABLE,
    f"BEFORE UPDATE ON {SIGHTING_TABLE} FOR EACH ROW",
    SIGHTINGS_FUNCTION,
)
_PG_TRIGGERS[f"{SIGHTING_TABLE}_no_delete"] = (
    SIGHTING_TABLE,
    f"BEFORE DELETE ON {SIGHTING_TABLE} FOR EACH ROW",
    "healthos_refuse_change",
)
_PG_TRIGGERS[f"{SIGHTING_TABLE}_no_truncate"] = (
    SIGHTING_TABLE,
    f"BEFORE TRUNCATE ON {SIGHTING_TABLE} FOR EACH STATEMENT",
    "healthos_refuse_change",
)

PROTECTIVE_TRIGGERS = tuple(
    (table, trigger, function) for trigger, (table, _, function) in _PG_TRIGGERS.items()
)
PG_TRIGGER_SQL = {
    trigger: (table, f"CREATE TRIGGER {trigger}\n{definition}\nEXECUTE FUNCTION {function}()")
    for trigger, (table, definition, function) in _PG_TRIGGERS.items()
}


def _sqlite_refusal(name: str, table: str, operation: str, when: str = "") -> str:
    condition = f"\nWHEN {when}" if when else ""
    message = (
        f"{table} only widens: {operation} refused"
        if when
        else f"{table} keeps every record: {operation} refused"
    )
    return f"""CREATE TRIGGER {name}
BEFORE {operation} ON {table}{condition}
BEGIN
    SELECT RAISE(ABORT, '{message}');
END"""


_SQLITE_TRIGGERS = {}
for _table in IDENTITY_TABLES:
    for _operation in ("UPDATE", "DELETE"):
        _name = f"{_table}_no_{_operation.lower()}"
        _SQLITE_TRIGGERS[_name] = (_table, _sqlite_refusal(_name, _table, _operation))
_SQLITE_TRIGGERS[f"{SIGHTING_TABLE}_no_delete"] = (
    SIGHTING_TABLE,
    _sqlite_refusal(f"{SIGHTING_TABLE}_no_delete", SIGHTING_TABLE, "DELETE"),
)
_SQLITE_TRIGGERS[f"{SIGHTING_TABLE}_widen_only"] = (
    SIGHTING_TABLE,
    _sqlite_refusal(
        f"{SIGHTING_TABLE}_widen_only",
        SIGHTING_TABLE,
        "UPDATE",
        "NEW.data_type IS NOT OLD.data_type OR NEW.fingerprint IS NOT OLD.fingerprint "
        "OR NEW.stable_class IS NOT OLD.stable_class OR NEW.label IS NOT OLD.label "
        "OR NEW.data_source IS NOT OLD.data_source OR NEW.first_seen_at > OLD.first_seen_at "
        "OR NEW.last_seen_at < OLD.last_seen_at OR NEW.newest_record_at < OLD.newest_record_at "
        "OR (OLD.newest_record_at IS NOT NULL AND NEW.newest_record_at IS NULL) "
        "OR (OLD.complete AND NOT NEW.complete)",
    ),
)
SQLITE_TRIGGER_SQL = dict(_SQLITE_TRIGGERS)

POSTGRESQL_DDL = (
    *(
        f"CREATE FUNCTION {name}() RETURNS trigger\nLANGUAGE plpgsql AS $${body}$$"
        for name, body in FUNCTION_BODIES.items()
    ),
    *(sql for _, sql in PG_TRIGGER_SQL.values()),
    *(
        f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {trigger}"
        for table, trigger, _ in PROTECTIVE_TRIGGERS
    ),
)
SQLITE_DDL = tuple(sql for _, sql in _SQLITE_TRIGGERS.values())

for _statement in POSTGRESQL_DDL:
    event.listen(Base.metadata, "after_create", DDL(_statement).execute_if(dialect="postgresql"))
for _statement in SQLITE_DDL:
    event.listen(Base.metadata, "after_create", DDL(_statement).execute_if(dialect="sqlite"))
