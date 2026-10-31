"""Let a signed webhook body that jsonb cannot hold be archived as its bytes alone.

PostgreSQL jsonb refuses U+0000, unpaired surrogates and non-finite numbers, all of
which Python's json module accepts. Until now such a verified body failed the
`webhook_events.raw_json` insert and the whole ingest rolled back, so its signed bytes
were never stored. From this revision `webhook_events.raw_json` and
`webhook_notifications.raw_json` may be NULL, under two checks:

* `ck_webhook_events_raw_kept`: a batch has raw_json or raw_body (its exact bytes);
* `ck_webhook_notifications_raw_or_rejected`: an element without raw_json is rejected.

No row is read, rewritten or deleted: every existing row has raw_json, so both checks
hold already. The 0011 triggers need no change: they compare raw_json with
`IS DISTINCT FROM` (PostgreSQL) and `IS NOT` (SQLite), so NULL to a value and back is
refused, and a set raw_body can never be cleared or replaced.

Dropping NOT NULL needs an ACCESS EXCLUSIVE lock on both tables (catalog-only, plus one
scan of each for its check), which a running pg_dump blocks for its whole run, unlike
0012-0020. So this revision waits at most `lock_timeout` (30 s, `-x lock-timeout=...`) and
then fails, changing nothing, with a message to retry once the dump has finished.

SQLite (tests only) cannot drop NOT NULL in place: both tables are rebuilt, which drops
their triggers; the frozen copies below put the 0011 triggers back unchanged.

downgrade() restores NOT NULL only while no row has a NULL raw_json; otherwise it raises,
changing nothing, because the only ways back would delete or rewrite received bodies.
Offline (`--sql`) it refuses outright.
"""

import re

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261031_0021"
down_revision = "20261025_0020"
branch_labels = None
depends_on = None

CHECKS = {
    "webhook_events": ("raw_kept", "raw_json IS NOT NULL OR raw_body IS NOT NULL"),
    "webhook_notifications": ("raw_or_rejected", "raw_json IS NOT NULL OR NOT is_valid"),
}
RAW_JSON = sa.JSON().with_variant(JSONB(), "postgresql")
# Frozen copy of the 0011 SQLite triggers on both tables (app/models/raw_versions.py).
SQLITE_TRIGGERS = (
    """CREATE TRIGGER webhook_events_frozen_payload
BEFORE UPDATE ON webhook_events FOR EACH ROW
WHEN NEW.payload_hash IS NOT OLD.payload_hash OR NEW.raw_json IS NOT OLD.raw_json OR \
NEW.created_at IS NOT OLD.created_at OR (OLD.raw_body IS NOT NULL AND OLD.raw_body IS NOT \
NEW.raw_body)
BEGIN
    SELECT RAISE(ABORT, 'webhook_events keeps every record: UPDATE refused');
END""",
    """CREATE TRIGGER webhook_events_no_delete
BEFORE DELETE ON webhook_events
BEGIN
    SELECT RAISE(ABORT, 'webhook_events keeps every record: DELETE refused');
END""",
    """CREATE TRIGGER webhook_notifications_frozen_payload
BEFORE UPDATE ON webhook_notifications FOR EACH ROW
WHEN NEW.element_hash IS NOT OLD.element_hash OR NEW.raw_json IS NOT OLD.raw_json OR \
NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'webhook_notifications keeps every record: UPDATE refused');
END""",
    """CREATE TRIGGER webhook_notifications_no_delete
BEFORE DELETE ON webhook_notifications
BEGIN
    SELECT RAISE(ABORT, 'webhook_notifications keeps every record: DELETE refused');
END""",
)
# How long either direction waits for both locks (see above); `alembic -x lock-timeout=2min
# upgrade head` overrides it.
LOCK_TIMEOUT = "30s"
_LOCK_NOT_AVAILABLE = "55P03"


def _lock_timeout() -> str:
    value = context.get_x_argument(as_dictionary=True).get("lock-timeout", LOCK_TIMEOUT)
    if not re.fullmatch(r"[1-9][0-9]*(ms|s|min)?", value):
        raise ValueError(f"lock-timeout must look like 30s, 500ms or 2min, not {value!r}")
    return value


def _locked(nullable: bool) -> None:
    """Lock both tables (PostgreSQL), refuse a downgrade over body-only rows, then alter."""
    postgresql = op.get_context().dialect.name == "postgresql"
    timeout = _lock_timeout()
    try:
        if postgresql:
            op.execute(f"SET LOCAL lock_timeout = '{timeout}'")
            op.execute(f"LOCK TABLE {', '.join(CHECKS)} IN ACCESS EXCLUSIVE MODE")
        if not nullable:
            _refuse_body_only_rows()
        _alter(nullable)
    except sa.exc.DBAPIError as exc:
        code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        if code != _LOCK_NOT_AVAILABLE:
            raise
        raise RuntimeError(
            f"20261031_0021 could not lock {' and '.join(CHECKS)} within {timeout}: another "
            "session holds them (most likely the nightly backup's pg_dump). Nothing was "
            "changed; retry when that session has finished (see docs/RUNBOOK.md, "
            "20261031_0021)."
        ) from exc
    if postgresql:
        op.execute("SET LOCAL lock_timeout TO DEFAULT")


def _refuse_body_only_rows() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("body-only webhook rows must be checked before an online downgrade")
    held = op.get_bind().scalar(
        sa.text(
            "SELECT (SELECT count(*) FROM webhook_events WHERE raw_json IS NULL) + "
            "(SELECT count(*) FROM webhook_notifications WHERE raw_json IS NULL)"
        )
    )
    if held:
        raise RuntimeError(
            f"webhook tables hold {held} body-only rows; downgrade refused (restoring NOT NULL "
            "would mean deleting or rewriting received webhook payloads)"
        )


def _alter(nullable: bool) -> None:
    sqlite = op.get_context().dialect.name == "sqlite"
    for table, (name, condition) in CHECKS.items():
        with op.batch_alter_table(table, recreate="always" if sqlite else "never") as batch:
            if not nullable:
                batch.drop_constraint(op.f(f"ck_{table}_{name}"), type_="check")
            batch.alter_column("raw_json", existing_type=RAW_JSON, nullable=nullable)
            if nullable:
                batch.create_check_constraint(op.f(f"ck_{table}_{name}"), condition)
    if sqlite:
        for statement in SQLITE_TRIGGERS:
            op.execute(statement)


def upgrade() -> None:
    _locked(nullable=True)


def downgrade() -> None:
    _locked(nullable=False)
