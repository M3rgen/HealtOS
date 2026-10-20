"""Keep every payload version of a raw record: `raw_health_data_versions`.

Until now a re-fetch that brought a different payload for an existing
`(data_type, google_data_point_id)` overwrote `raw_health_data.raw_json`, and the previous
payload was gone. From this revision on:

* `raw_health_data` still holds the current version of every record (all readers stay as
  they are). It gains two nullable columns, `payload_received_at` and `payload_source`:
  when and through which path (sync, webhook, reconciliation, backfill) the current
  payload arrived. NULL means "stored before this revision".
* A `BEFORE UPDATE` trigger on `raw_health_data` copies the row being replaced into the
  new append-only table `raw_health_data_versions` whenever `payload_hash` changes, in the
  same statement as the overwrite: the old payload is kept even if the batch crashes
  (both roll back) or two writers race (the trigger runs under the row lock and sees the
  row it really replaces). The same payload received again writes nothing.
* `raw_health_data_versions` refuses UPDATE (except renumbering `version`), DELETE and
  TRUNCATE through triggers; its foreign key keeps the current row from being deleted.
* The view `raw_health_data_history` lists every version of every record (history rows
  plus the current row) with version number, `valid_from`/`valid_to` and `is_current`.

Seeding: a record without history rows *is* version 1, and the view numbers the current
row `max(history version) + 1`. So the existing archive needs no copy: no row of
`raw_health_data` is rewritten, nothing is stored twice, and the upgrade is catalog-only
(nullable `ADD COLUMN` without a default, an empty table, two functions, three triggers,
one view): well under a second, independent of table size, with no extra disk. The
history then grows only by the payloads that Google actually replaces (a few thousand
rows a day at most).

downgrade() drops only these new objects. It refuses while history rows exist, because
dropping them would lose exactly the versions this revision keeps; pass
`-x allow-version-loss=1` to downgrade anyway (after a backup).

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

VIEW_SQL = """CREATE VIEW raw_health_data_history AS
SELECT v.data_type, v.google_data_point_id, v.raw_id, v.version, (1 = 0) AS is_current,
       v.raw_json, v.payload_hash, v.source,
       coalesce(v.received_at, CASE WHEN v.version = 1 THEN r.first_seen_at END) AS valid_from,
       v.superseded_at AS valid_to, v.last_seen_at, v.source_start_time, v.source_end_time,
       NULL AS source_deleted_at, v.id AS history_id
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
       NULL, r.last_seen_at, r.source_start_time, r.source_end_time, r.source_deleted_at, NULL
FROM raw_health_data r"""

_COPY = (
    "raw_id, data_type, google_data_point_id, version, raw_json, payload_hash, "
    "source_start_time, source_end_time, source_timezone, received_at, last_seen_at, "
    "superseded_at, source"
)
_OLD = (
    "OLD.id, OLD.data_type, OLD.google_data_point_id, "
    "(SELECT coalesce(max(v.version), 0) + 1 FROM raw_health_data_versions v "
    "WHERE v.raw_id = OLD.id), "
    "OLD.raw_json, OLD.payload_hash, OLD.source_start_time, OLD.source_end_time, "
    "OLD.source_timezone, OLD.payload_received_at, OLD.last_seen_at, NEW.last_seen_at, "
    "coalesce(OLD.payload_source, 'legacy')"
)
_FROZEN = (
    "id",
    "raw_id",
    "data_type",
    "google_data_point_id",
    "raw_json",
    "payload_hash",
    "source_start_time",
    "source_end_time",
    "source_timezone",
    "received_at",
    "last_seen_at",
    "superseded_at",
    "source",
    "created_at",
)

POSTGRESQL_DDL = (
    f"""CREATE FUNCTION raw_health_data_keep_version() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO raw_health_data_versions ({_COPY})
    VALUES ({_OLD});
    RETURN NEW;
END
$$""",
    """CREATE TRIGGER raw_health_data_keep_version
BEFORE UPDATE ON raw_health_data FOR EACH ROW
WHEN (OLD.payload_hash IS DISTINCT FROM NEW.payload_hash)
EXECUTE FUNCTION raw_health_data_keep_version()""",
    f"""CREATE FUNCTION raw_health_data_versions_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF ({", ".join(f"NEW.{c}" for c in _FROZEN)})
           IS NOT DISTINCT FROM ({", ".join(f"OLD.{c}" for c in _FROZEN)}) THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION USING MESSAGE =
        'raw_health_data_versions is append-only: ' || TG_OP || ' refused';
END
$$""",
    """CREATE TRIGGER raw_health_data_versions_append_only
BEFORE UPDATE OR DELETE ON raw_health_data_versions FOR EACH ROW
EXECUTE FUNCTION raw_health_data_versions_append_only()""",
    """CREATE TRIGGER raw_health_data_versions_no_truncate
BEFORE TRUNCATE ON raw_health_data_versions FOR EACH STATEMENT
EXECUTE FUNCTION raw_health_data_versions_append_only()""",
    VIEW_SQL,
)

SQLITE_DDL = (
    f"""CREATE TRIGGER raw_health_data_keep_version
BEFORE UPDATE ON raw_health_data FOR EACH ROW
WHEN OLD.payload_hash IS NOT NEW.payload_hash
BEGIN
    INSERT INTO raw_health_data_versions ({_COPY})
    VALUES ({_OLD});
END""",
    """CREATE TRIGGER raw_health_data_versions_no_delete
BEFORE DELETE ON raw_health_data_versions
BEGIN
    SELECT RAISE(ABORT, 'raw_health_data_versions is append-only: DELETE refused');
END""",
    f"""CREATE TRIGGER raw_health_data_versions_no_update
BEFORE UPDATE ON raw_health_data_versions FOR EACH ROW
WHEN {" OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in _FROZEN)}
BEGIN
    SELECT RAISE(ABORT, 'raw_health_data_versions is append-only: UPDATE refused');
END""",
    VIEW_SQL,
)


def upgrade() -> None:
    op.add_column(
        "raw_health_data",
        sa.Column("payload_received_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("raw_health_data", sa.Column("payload_source", sa.String(32), nullable=True))
    op.create_table(
        "raw_health_data_versions",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
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
        sa.ForeignKeyConstraint(
            ["raw_id"],
            ["raw_health_data.id"],
            name="fk_raw_health_data_versions_raw_id_raw_health_data",
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
    dialect = op.get_context().dialect.name
    for statement in POSTGRESQL_DDL if dialect == "postgresql" else SQLITE_DDL:
        op.execute(statement)


def downgrade() -> None:
    context_ = op.get_context()
    allow_loss = context.get_x_argument(as_dictionary=True).get("allow-version-loss") == "1"
    if not context_.as_sql and not allow_loss:
        kept = op.get_bind().scalar(sa.text("SELECT count(*) FROM raw_health_data_versions"))
        if kept:
            raise RuntimeError(
                f"raw_health_data_versions holds {kept} earlier payload version(s); "
                "downgrading would drop them. Take a backup, then rerun with "
                "-x allow-version-loss=1"
            )
    op.execute("DROP VIEW raw_health_data_history")
    if context_.dialect.name == "postgresql":
        op.execute("DROP TRIGGER raw_health_data_keep_version ON raw_health_data")
        op.execute("DROP FUNCTION raw_health_data_keep_version()")
    else:
        op.execute("DROP TRIGGER raw_health_data_keep_version")
    # Dropping the table drops its own triggers; then the PostgreSQL guard function.
    op.drop_table("raw_health_data_versions")
    if context_.dialect.name == "postgresql":
        op.execute("DROP FUNCTION raw_health_data_versions_append_only()")
    op.drop_column("raw_health_data", "payload_source")
    op.drop_column("raw_health_data", "payload_received_at")
