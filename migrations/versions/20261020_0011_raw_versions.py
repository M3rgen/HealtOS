"""Keep every payload version of a raw record and refuse deleting any record.

Until now a re-fetch that brought a different payload for an existing
`(data_type, google_data_point_id)` overwrote `raw_health_data.raw_json`, and the previous
payload was gone; nothing in the database stopped a `DELETE` either. From this revision on:

* `raw_health_data` still holds the current version of every record (all readers stay as
  they are). It gains two nullable columns, `payload_received_at` and `payload_source`:
  when and through which path (sync, webhook, reconciliation, backfill, gap-repair,
  archive-import) the current payload arrived. NULL means "stored before this revision".
* A `BEFORE UPDATE` trigger on `raw_health_data` copies the row being replaced into the
  new append-only table `raw_health_data_versions` whenever `payload_hash` *or* the stored
  text (`raw_json::text`) changes, in the same statement as the overwrite: the old payload
  is kept even if the batch crashes (both roll back) or two writers race (the trigger runs
  under the row lock and sees the row it really replaces). The same payload received
  again writes nothing.
* Triggers refuse `DELETE` and `TRUNCATE` on `raw_health_data` (a `TRUNCATE ... CASCADE`
  from any referencing table reaches the same trigger) and refuse changing a record's
  identity columns. `raw_health_data_versions` refuses `UPDATE`, `DELETE` and `TRUNCATE`;
  the one exception is renumbering `version`, and on PostgreSQL only in a transaction that
  set `healthos.renumber = '1'` (only `health-mirror import-versions` does). raw_json is
  compared as text, so even a respelled number counts as a change.
* Every protective trigger is `ENABLE ALWAYS`: it also fires with
  `session_replication_role = replica`. `health-mirror verify` fails when one is missing,
  disabled, or its function no longer has the body below.
* The view `raw_health_data_history` lists every version of every record (history rows
  plus the current row) with version number, `valid_from`/`valid_to` and `is_current`.

Seeding: a record without history rows *is* version 1, and the view numbers the current
row `max(history version) + 1`. So the existing archive needs no copy: no row of
`raw_health_data` is rewritten, nothing is stored twice, and the upgrade is catalog-only
(nullable `ADD COLUMN` without a default, an empty table, functions, triggers, one view):
well under a second, independent of table size, with no extra disk.

downgrade() drops only these new objects. It refuses while history rows exist, because
dropping them would lose exactly the versions this revision keeps; pass
`-x allow-version-loss=1` to downgrade anyway (after a backup). Offline (`--sql`) the
check is emitted as a `DO` block that raises in the generated script itself.

The trigger and view SQL below is a frozen copy of app/models/raw_versions.py, which
builds the same objects for `create_all`; tests/test_raw_versions.py compares them.
"""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261020_0011"
down_revision = "20261020_0010"
branch_labels = None
depends_on = None

HISTORY_VIEW = "raw_health_data_history"
RENUMBER_SETTING = "healthos.renumber"
# Tables whose rows a downgrade would drop; it refuses while any of them has rows.
KEPT_TABLES = ("raw_health_data_versions", "raw_health_data_events", "raw_api_responses")
BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

# Columns copied from the replaced raw row; the trigger SQL lists them in this order.
_COPY = (
    "raw_id, data_type, google_data_point_id, version, raw_json, payload_hash, "
    "source_start_time, source_end_time, source_timezone, received_at, last_seen_at, "
    "superseded_at, source, response_id"
)
_OLD = (
    "OLD.id, OLD.data_type, OLD.google_data_point_id, "
    "(SELECT coalesce(max(v.version), 0) + 1 FROM raw_health_data_versions v "
    "WHERE v.raw_id = OLD.id), "
    "OLD.raw_json, OLD.payload_hash, OLD.source_start_time, OLD.source_end_time, "
    "OLD.source_timezone, OLD.payload_received_at, OLD.last_seen_at, NEW.last_seen_at, "
    "coalesce(OLD.payload_source, 'legacy'), OLD.response_id"
)
# Every history column except `version`: these may never change. raw_json is compared as
# text, so a respelled number (1.0 for 1) counts as a change.
_FROZEN = (
    "id",
    "raw_id",
    "data_type",
    "google_data_point_id",
    "raw_json::text",
    "payload_hash",
    "source_start_time",
    "source_end_time",
    "source_timezone",
    "received_at",
    "last_seen_at",
    "superseded_at",
    "source",
    "created_at",
    "response_id",
)
# A raw record's identity: never rewritten (a new key is a new record).
_IDENTITY = ("id", "data_type", "google_data_point_id", "first_seen_at", "created_at")
_EVENT = (
    "INSERT INTO raw_health_data_events "
    "(raw_id, data_type, google_data_point_id, event, occurred_at, previous_deleted_at) "
    "VALUES (NEW.id, NEW.data_type, NEW.google_data_point_id, "
    "CASE WHEN NEW.source_deleted_at IS NULL THEN 'source-returned' "
    "ELSE 'source-deleted' END, "
    "coalesce(NEW.source_deleted_at, NEW.last_seen_at), {previous})"
)
# Tables whose rows never change once written.
APPEND_ONLY_TABLES = ("raw_api_responses", "raw_health_data_events")


def _row(prefix: str, columns) -> str:
    return ", ".join(f"{prefix}.{column}" for column in columns)


def _sqlite_changed(columns) -> str:
    return " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in (c.split("::")[0] for c in columns))


FUNCTION_BODIES = {
    "healthos_refuse_change": """
BEGIN
    RAISE EXCEPTION USING MESSAGE =
        TG_TABLE_NAME || ' keeps every record: ' || TG_OP || ' refused';
END
""",
    "raw_health_data_keep_version": f"""
BEGIN
    INSERT INTO raw_health_data_versions ({_COPY})
    VALUES ({_OLD});
    RETURN NEW;
END
""",
    "raw_health_data_versions_append_only": f"""
BEGIN
    IF TG_OP = 'UPDATE'
       AND current_setting('{RENUMBER_SETTING}', true) = '1'
       AND ({_row("NEW", _FROZEN)}) IS NOT DISTINCT FROM ({_row("OLD", _FROZEN)}) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION USING MESSAGE =
        'raw_health_data_versions is append-only: ' || TG_OP || ' refused';
END
""",
    "raw_health_data_record_event": f"""
BEGIN
    {_EVENT.format(previous="CASE WHEN TG_OP = 'UPDATE' THEN OLD.source_deleted_at END")};
    RETURN NULL;
END
""",
}

# trigger -> (table, timing/events/level/WHEN, function) for every protective PostgreSQL
# trigger; health-mirror verify checks each one.
_PG_TRIGGERS = {
    "raw_health_data_keep_version": (
        "raw_health_data",
        """BEFORE UPDATE ON raw_health_data FOR EACH ROW
WHEN (OLD.payload_hash IS DISTINCT FROM NEW.payload_hash
      OR OLD.raw_json::text IS DISTINCT FROM NEW.raw_json::text)""",
        "raw_health_data_keep_version",
    ),
    "raw_health_data_frozen_identity": (
        "raw_health_data",
        f"""BEFORE UPDATE ON raw_health_data FOR EACH ROW
WHEN (({_row("OLD", _IDENTITY)}) IS DISTINCT FROM ({_row("NEW", _IDENTITY)}))""",
        "healthos_refuse_change",
    ),
    "raw_health_data_no_delete": (
        "raw_health_data",
        "BEFORE DELETE ON raw_health_data FOR EACH ROW",
        "healthos_refuse_change",
    ),
    "raw_health_data_no_truncate": (
        "raw_health_data",
        "BEFORE TRUNCATE ON raw_health_data FOR EACH STATEMENT",
        "healthos_refuse_change",
    ),
    "raw_health_data_deletion_event": (
        "raw_health_data",
        """AFTER UPDATE ON raw_health_data FOR EACH ROW
WHEN (OLD.source_deleted_at IS DISTINCT FROM NEW.source_deleted_at)""",
        "raw_health_data_record_event",
    ),
    "raw_health_data_restored_event": (
        "raw_health_data",
        """AFTER INSERT ON raw_health_data FOR EACH ROW
WHEN (NEW.source_deleted_at IS NOT NULL)""",
        "raw_health_data_record_event",
    ),
    "raw_health_data_versions_append_only": (
        "raw_health_data_versions",
        "BEFORE UPDATE OR DELETE ON raw_health_data_versions FOR EACH ROW",
        "raw_health_data_versions_append_only",
    ),
    "raw_health_data_versions_no_truncate": (
        "raw_health_data_versions",
        "BEFORE TRUNCATE ON raw_health_data_versions FOR EACH STATEMENT",
        "healthos_refuse_change",
    ),
}
for _table in APPEND_ONLY_TABLES:
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

# (table, trigger, function) of every protective PostgreSQL trigger.
PROTECTIVE_TRIGGERS = tuple(
    (table, trigger, function) for trigger, (table, _, function) in _PG_TRIGGERS.items()
)

VIEW_SQL = f"""CREATE VIEW {HISTORY_VIEW} AS
SELECT v.data_type, v.google_data_point_id, v.raw_id, v.version, (1 = 0) AS is_current,
       v.raw_json, v.payload_hash, v.source,
       coalesce(v.received_at, CASE WHEN v.version = 1 THEN r.first_seen_at END) AS valid_from,
       v.superseded_at AS valid_to, v.last_seen_at, v.source_start_time, v.source_end_time,
       NULL AS source_deleted_at, v.id AS history_id, v.response_id
FROM raw_health_data_versions v
JOIN raw_health_data r ON r.id = v.raw_id
UNION ALL
SELECT r.data_type, r.google_data_point_id, r.id,
       coalesce((SELECT max(h.version) FROM raw_health_data_versions h WHERE h.raw_id = r.id), 0)
       + 1,
       (1 = 1), r.raw_json, r.payload_hash, coalesce(r.payload_source, 'legacy'),
       coalesce(
           r.payload_received_at,
           (SELECT max(h.superseded_at) FROM raw_health_data_versions h WHERE h.raw_id = r.id),
           CASE WHEN NOT EXISTS (SELECT 1 FROM raw_health_data_versions h WHERE h.raw_id = r.id)
                THEN r.first_seen_at END),
       NULL, r.last_seen_at, r.source_start_time, r.source_end_time, r.source_deleted_at, NULL,
       r.response_id
FROM raw_health_data r"""

POSTGRESQL_DDL = (
    *(
        f"CREATE FUNCTION {name}() RETURNS trigger\nLANGUAGE plpgsql AS $${body}$$"
        for name, body in FUNCTION_BODIES.items()
    ),
    *(
        f"CREATE TRIGGER {trigger}\n{definition}\nEXECUTE FUNCTION {function}()"
        for trigger, (_, definition, function) in _PG_TRIGGERS.items()
    ),
    *(
        f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {trigger}"
        for table, trigger, _ in PROTECTIVE_TRIGGERS
    ),
    VIEW_SQL,
)


def _sqlite_refuse(table: str, operation: str) -> str:
    return f"SELECT RAISE(ABORT, '{table} keeps every record: {operation} refused');"


# trigger -> (table, full SQL) of every protective SQLite trigger (SQLite cannot disable
# triggers, so verify only checks that they exist).
_SQLITE_TRIGGERS = {
    "raw_health_data_keep_version": (
        "raw_health_data",
        f"""CREATE TRIGGER raw_health_data_keep_version
BEFORE UPDATE ON raw_health_data FOR EACH ROW
WHEN OLD.payload_hash IS NOT NEW.payload_hash OR OLD.raw_json IS NOT NEW.raw_json
BEGIN
    INSERT INTO raw_health_data_versions ({_COPY})
    VALUES ({_OLD});
END""",
    ),
    "raw_health_data_frozen_identity": (
        "raw_health_data",
        f"""CREATE TRIGGER raw_health_data_frozen_identity
BEFORE UPDATE ON raw_health_data FOR EACH ROW
WHEN {_sqlite_changed(_IDENTITY)}
BEGIN
    {_sqlite_refuse("raw_health_data", "UPDATE")}
END""",
    ),
    "raw_health_data_no_delete": (
        "raw_health_data",
        f"""CREATE TRIGGER raw_health_data_no_delete
BEFORE DELETE ON raw_health_data
BEGIN
    {_sqlite_refuse("raw_health_data", "DELETE")}
END""",
    ),
    "raw_health_data_deletion_event": (
        "raw_health_data",
        f"""CREATE TRIGGER raw_health_data_deletion_event
AFTER UPDATE ON raw_health_data FOR EACH ROW
WHEN OLD.source_deleted_at IS NOT NEW.source_deleted_at
BEGIN
    {_EVENT.format(previous="OLD.source_deleted_at")};
END""",
    ),
    "raw_health_data_restored_event": (
        "raw_health_data",
        f"""CREATE TRIGGER raw_health_data_restored_event
AFTER INSERT ON raw_health_data FOR EACH ROW
WHEN NEW.source_deleted_at IS NOT NULL
BEGIN
    {_EVENT.format(previous="NULL")};
END""",
    ),
    "raw_health_data_versions_no_delete": (
        "raw_health_data_versions",
        """CREATE TRIGGER raw_health_data_versions_no_delete
BEFORE DELETE ON raw_health_data_versions
BEGIN
    SELECT RAISE(ABORT, 'raw_health_data_versions is append-only: DELETE refused');
END""",
    ),
    # SQLite (tests only) has no session settings: renumbering is not gated there.
    "raw_health_data_versions_no_update": (
        "raw_health_data_versions",
        f"""CREATE TRIGGER raw_health_data_versions_no_update
BEFORE UPDATE ON raw_health_data_versions FOR EACH ROW
WHEN {_sqlite_changed(_FROZEN)}
BEGIN
    SELECT RAISE(ABORT, 'raw_health_data_versions is append-only: UPDATE refused');
END""",
    ),
}
for _table in APPEND_ONLY_TABLES:
    for _operation in ("UPDATE", "DELETE"):
        _name = f"{_table}_no_{_operation.lower()}"
        _SQLITE_TRIGGERS[_name] = (
            _table,
            f"""CREATE TRIGGER {_name}
BEFORE {_operation} ON {_table}
BEGIN
    {_sqlite_refuse(_table, _operation)}
END""",
        )

SQLITE_TRIGGERS = tuple((table, trigger) for trigger, (table, _) in _SQLITE_TRIGGERS.items())
SQLITE_DDL = (*(sql for _, sql in _SQLITE_TRIGGERS.values()), VIEW_SQL)


def upgrade() -> None:
    dialect = op.get_context().dialect.name
    op.create_table(
        "raw_api_responses",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("context_id", sa.String(160), nullable=True),
        sa.Column("data_type", sa.String(128), nullable=True),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column("request_body", sa.LargeBinary(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("headers", sa.JSON(), nullable=False),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("body_size", sa.BigInteger(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("error", sa.String(128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_raw_api_responses"),
    )
    op.create_index(
        "ix_raw_api_responses_type_fetched", "raw_api_responses", ["data_type", "fetched_at"]
    )
    op.create_index("ix_raw_api_responses_context_id", "raw_api_responses", ["context_id"])

    op.add_column(
        "raw_health_data",
        sa.Column("payload_received_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("raw_health_data", sa.Column("payload_source", sa.String(32), nullable=True))
    if dialect == "postgresql":
        op.add_column("raw_health_data", sa.Column("response_id", sa.BigInteger(), nullable=True))
        # NOT VALID: catalog-only (no scan of the existing, all-NULL rows); every row
        # written from now on is checked.
        op.execute(
            "ALTER TABLE raw_health_data ADD CONSTRAINT "
            "fk_raw_health_data_response_id_raw_api_responses FOREIGN KEY (response_id) "
            "REFERENCES raw_api_responses (id) NOT VALID"
        )
    else:
        # SQLite (tests) cannot add a constraint later, only a column that carries one.
        op.execute(
            "ALTER TABLE raw_health_data ADD COLUMN response_id INTEGER "
            "CONSTRAINT fk_raw_health_data_response_id_raw_api_responses "
            "REFERENCES raw_api_responses (id)"
        )
    op.create_table(
        "raw_health_data_versions",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("raw_id", sa.Uuid(), nullable=False),
        sa.Column("data_type", sa.String(128), nullable=False),
        sa.Column("google_data_point_id", sa.String(512), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "raw_json",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("source_start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_end_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_timezone", sa.String(128), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("response_id", BIGINT, nullable=True),
        sa.ForeignKeyConstraint(
            ["raw_id"],
            ["raw_health_data.id"],
            name="fk_raw_health_data_versions_raw_id_raw_health_data",
        ),
        sa.ForeignKeyConstraint(
            ["response_id"],
            ["raw_api_responses.id"],
            name="fk_raw_health_data_versions_response_id_raw_api_responses",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_raw_health_data_versions"),
        sa.UniqueConstraint("raw_id", "version", name="uq_raw_health_data_versions_raw_id"),
    )
    op.create_index(
        "ix_raw_health_data_versions_key",
        "raw_health_data_versions",
        ["data_type", "google_data_point_id"],
    )
    op.create_index(
        "ix_raw_health_data_versions_superseded_at",
        "raw_health_data_versions",
        ["superseded_at"],
    )
    op.create_table(
        "raw_health_data_events",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("raw_id", sa.Uuid(), nullable=False),
        sa.Column("data_type", sa.String(128), nullable=False),
        sa.Column("google_data_point_id", sa.String(512), nullable=False),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["raw_id"],
            ["raw_health_data.id"],
            name="fk_raw_health_data_events_raw_id_raw_health_data",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_raw_health_data_events"),
    )
    op.create_index("ix_raw_health_data_events_raw_id", "raw_health_data_events", ["raw_id"])
    for statement in POSTGRESQL_DDL if dialect == "postgresql" else SQLITE_DDL:
        op.execute(statement)


def _refuse_while_kept(context_) -> None:
    allow_loss = context.get_x_argument(as_dictionary=True).get("allow-version-loss") == "1"
    if allow_loss:
        return
    hint = "Take a backup, then rerun with -x allow-version-loss=1"
    if context_.as_sql:
        if context_.dialect.name != "postgresql":
            raise RuntimeError(f"offline downgrade cannot check {', '.join(KEPT_TABLES)}; {hint}")
        # The generated script must not drop kept rows either: it checks when it runs.
        for table in KEPT_TABLES:
            op.execute(
                f"""DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM {table}) THEN
        RAISE EXCEPTION '{table} holds rows that downgrading would drop; {hint}';
    END IF;
END
$$"""
            )
        return
    for table in KEPT_TABLES:
        kept = op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table}"))
        if kept:
            raise RuntimeError(f"{table} holds {kept} row(s); downgrading would drop them. {hint}")


def downgrade() -> None:
    context_ = op.get_context()
    _refuse_while_kept(context_)
    postgresql = context_.dialect.name == "postgresql"
    op.execute(f"DROP VIEW {HISTORY_VIEW}")
    if postgresql:
        for table, trigger, _ in PROTECTIVE_TRIGGERS:
            if table == "raw_health_data":
                op.execute(f"DROP TRIGGER {trigger} ON {table}")
    else:
        for table, trigger in SQLITE_TRIGGERS:
            if table == "raw_health_data":
                op.execute(f"DROP TRIGGER {trigger}")
    # Dropping a table drops its own triggers; then the PostgreSQL functions.
    op.drop_table("raw_health_data_events")
    op.drop_table("raw_health_data_versions")
    # Drops its foreign key too (PostgreSQL; SQLite 3.35+ with the inline constraint).
    op.execute("ALTER TABLE raw_health_data DROP COLUMN response_id")
    op.drop_table("raw_api_responses")
    if postgresql:
        for name in FUNCTION_BODIES:
            op.execute(f"DROP FUNCTION {name}()")
    op.drop_column("raw_health_data", "payload_source")
    op.drop_column("raw_health_data", "payload_received_at")
