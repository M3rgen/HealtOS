"""Transactional dirty revisions, including SQL bulk writers and identity undo.

The trigger DDL is frozen in migrations 0018/0020. Only disposable derived rows are updated.
A shared PostgreSQL advisory transaction lock fences mutations against a builder's
exclusive lock: even a previously unseen day cannot miss a concurrently committed change.
Unrelated raw and identity data types bypass that fence before entering the function.
SQLite's single writer provides the same fence when the builder inserts its day first.
"""

from sqlalchemy import DDL, event

from app.db.base import Base

BUILD_LOCK = 72184518
DAILY = "'daily-resting-heart-rate','daily-heart-rate-variability','daily-oxygen-saturation'"
DERIVED_TYPES = f"'steps','sleep',{DAILY}"
TYPE_FILTERED_TABLES = {"raw_health_data", "raw_health_data_supersessions", "raw_source_devices"}


def _scope(ref):
    return f"""(
      ({ref}.data_type = 'steps' AND {ref}.source_start_time < window_stop
        AND {ref}.source_end_time > window_start)
      OR ({ref}.data_type = 'sleep' AND {ref}.source_end_time >= window_start
        AND {ref}.source_end_time < window_stop)
      OR ({ref}.data_type IN ({DAILY}) AND {ref}.source_start_time >= civil_start
        AND {ref}.source_start_time < civil_stop)
      OR ({ref}.data_type = 'steps' AND EXISTS (SELECT 1 FROM steps n
        WHERE n.google_data_point_id = {ref}.google_data_point_id
          AND n.start_time < window_stop AND n.end_time > window_start))
      OR ({ref}.data_type = 'sleep' AND EXISTS (SELECT 1 FROM sleep_sessions n
        WHERE n.google_data_point_id = {ref}.google_data_point_id
          AND n.end_time >= window_start AND n.end_time < window_stop)))"""


def _held(ref):
    return f"""EXISTS (SELECT 1 FROM raw_health_data r
      JOIN raw_health_data_supersessions s ON s.old_raw_id = r.id
      WHERE s.new_raw_id = {ref}.id AND {_scope("r")})"""


# table -> (operations, value-bearing columns, affected day predicate per OLD/NEW)
SPECS = {
    "raw_health_data": (
        ("INSERT", "UPDATE"),
        (
            "raw_json",
            "source_start_time",
            "source_end_time",
            "source_timezone",
            "source_deleted_at",
            "is_quarantined",
        ),
        lambda ref: f"({_scope(ref)} OR {_held(ref)})",
    ),
    "steps": (
        ("INSERT", "UPDATE", "DELETE"),
        ("start_time", "end_time", "count", "is_quarantined", "google_data_point_id"),
        lambda ref: f"{ref}.start_time < window_stop AND {ref}.end_time > window_start",
    ),
    "sleep_sessions": (
        ("INSERT", "UPDATE", "DELETE"),
        (
            "start_time",
            "end_time",
            "duration_seconds",
            "awake_seconds",
            "is_quarantined",
            "google_data_point_id",
        ),
        lambda ref: f"{ref}.end_time >= window_start AND {ref}.end_time < window_stop",
    ),
    "raw_health_data_supersessions": (
        ("INSERT",),
        (),
        lambda ref: (
            f"EXISTS (SELECT 1 FROM raw_health_data r WHERE "
            f"r.id IN ({ref}.old_raw_id, {ref}.new_raw_id) AND {_scope('r')})"
        ),
    ),
    "raw_source_devices": (
        ("INSERT",),
        (),
        lambda ref: (
            f"""EXISTS (SELECT 1 FROM raw_health_data r
          JOIN raw_health_data_supersessions s ON s.old_raw_id = r.id
          WHERE s.data_type = {ref}.data_type AND
            (s.old_fingerprint = {ref}.fingerprint OR s.new_fingerprint = {ref}.fingerprint)
            AND {_scope("r")})"""
        ),
    ),
    "manual_measurements": (
        ("INSERT",),
        (),
        lambda ref: f"{ref}.measured_at < window_stop",
    ),
    "manual_measurement_flag_events": (
        ("INSERT",),
        (),
        lambda ref: (
            f"EXISTS (SELECT 1 FROM manual_measurements m "
            f"WHERE m.id = {ref}.measurement_id AND m.measured_at < window_stop)"
        ),
    ),
}
for _table, _types in (
    ("heart_rate_variability", "'daily-heart-rate-variability'"),
    ("oxygen_saturation", "'daily-oxygen-saturation'"),
    ("resting_heart_rate", "'daily-resting-heart-rate'"),
):
    SPECS[_table] = (
        ("INSERT", "UPDATE", "DELETE"),
        ("is_quarantined", "google_data_point_id"),
        lambda ref, kinds=_types: (
            f"EXISTS (SELECT 1 FROM raw_health_data r WHERE "
            f"r.data_type IN ({kinds}) AND r.google_data_point_id = {ref}.google_data_point_id "
            f"AND {_scope('r')})"
        ),
    )


def statements():
    pg_triggers, sqlite_triggers, functions = {}, {}, {}
    for table, (operations, columns, affected) in SPECS.items():
        for operation in operations:
            name = f"derived_{table}_{operation.lower()}"
            refs = (
                ("OLD", "NEW")
                if operation == "UPDATE"
                else (("OLD",) if operation == "DELETE" else ("NEW",))
            )
            predicate = " OR ".join(f"({affected(ref)})" for ref in refs)
            body = f"UPDATE derived_days SET revision = revision + 1 WHERE {predicate};"
            changed = " OR ".join(
                f"NEW.{column} IS DISTINCT FROM OLD.{column}" for column in columns
            )
            condition = f" WHEN ({changed})" if operation == "UPDATE" else ""
            pg_condition = condition
            if table in TYPE_FILTERED_TABLES:
                # Only UPDATE has both rows. Check either side without narrowing the
                # existing value-change test; INSERT must never reference OLD.
                kinds = " OR ".join(f"{ref}.data_type IN ({DERIVED_TYPES})" for ref in refs)
                pg_condition = f" WHEN (({kinds})" + (
                    f" AND ({changed}))" if operation == "UPDATE" else ")"
                )
            function = f"healthos_{name}"
            functions[function] = (
                f"\nBEGIN\n    PERFORM pg_advisory_xact_lock_shared({BUILD_LOCK});\n    {body}\n"
                "    RETURN NULL;\nEND\n"
            )
            pg_triggers[name] = (
                table,
                f"CREATE TRIGGER {name} AFTER {operation} ON {table} FOR EACH ROW{pg_condition} "
                f"EXECUTE FUNCTION {function}()",
            )
            sqlite_condition = condition.replace(" IS DISTINCT FROM ", " IS NOT ")
            sqlite_triggers[name] = (
                table,
                f"CREATE TRIGGER {name} AFTER {operation} ON {table}{sqlite_condition} "
                f"BEGIN {body} END",
            )
    return pg_triggers, sqlite_triggers, functions


PG_TRIGGER_SQL, SQLITE_TRIGGER_SQL, FUNCTION_BODIES = statements()
PROTECTIVE_TRIGGERS = tuple(
    (table, name, f"healthos_{name}") for name, (table, _) in PG_TRIGGER_SQL.items()
)
POSTGRESQL_DDL = (
    *(
        f"CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $${body}$$"
        for name, body in FUNCTION_BODIES.items()
    ),
    *(sql for _, sql in PG_TRIGGER_SQL.values()),
    *(
        f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {name}"
        for table, name, _ in PROTECTIVE_TRIGGERS
    ),
)
SQLITE_DDL = tuple(sql for _, sql in SQLITE_TRIGGER_SQL.values())
for _statement in POSTGRESQL_DDL:
    event.listen(Base.metadata, "after_create", DDL(_statement).execute_if(dialect="postgresql"))
for _statement in SQLITE_DDL:
    event.listen(Base.metadata, "after_create", DDL(_statement).execute_if(dialect="sqlite"))
