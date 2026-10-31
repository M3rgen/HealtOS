"""Protect existing webhook diagnostics without changing any evidence.

Frozen SQL, independent of application modules. The shared refusal function belongs
to 0011; this revision only installs guards and its downgrade only removes them.
"""

from alembic import op

revision = "20261025_0019"
down_revision = "20261025_0018"
branch_labels = None
depends_on = None

POSTGRESQL_DDL = (
    """CREATE TRIGGER webhook_status_events_append_only
BEFORE UPDATE OR DELETE ON webhook_status_events FOR EACH ROW
EXECUTE FUNCTION healthos_refuse_change()""",
    """CREATE TRIGGER webhook_status_events_no_truncate
BEFORE TRUNCATE ON webhook_status_events FOR EACH STATEMENT
EXECUTE FUNCTION healthos_refuse_change()""",
    "ALTER TABLE webhook_status_events ENABLE ALWAYS TRIGGER webhook_status_events_append_only",
    "ALTER TABLE webhook_status_events ENABLE ALWAYS TRIGGER webhook_status_events_no_truncate",
)
SQLITE_DDL = (
    """CREATE TRIGGER webhook_status_events_no_update
BEFORE UPDATE ON webhook_status_events
BEGIN
    SELECT RAISE(ABORT, 'webhook_status_events keeps every record: UPDATE refused');
END""",
    """CREATE TRIGGER webhook_status_events_no_delete
BEFORE DELETE ON webhook_status_events
BEGIN
    SELECT RAISE(ABORT, 'webhook_status_events keeps every record: DELETE refused');
END""",
)


def upgrade() -> None:
    dialect = op.get_context().dialect.name
    for statement in POSTGRESQL_DDL if dialect == "postgresql" else SQLITE_DDL:
        op.execute(statement)


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP TRIGGER webhook_status_events_append_only ON webhook_status_events")
        op.execute("DROP TRIGGER webhook_status_events_no_truncate ON webhook_status_events")
    else:
        op.execute("DROP TRIGGER webhook_status_events_no_update")
        op.execute("DROP TRIGGER webhook_status_events_no_delete")
