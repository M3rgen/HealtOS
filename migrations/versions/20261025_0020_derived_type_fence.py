"""Fence only daily adapter inputs, preserving every source row and existing function.

Frozen PostgreSQL 17 SQL, independent of application modules. SQLite has no advisory
lock and retains its 0018 invalidation triggers. Both directions restore ALWAYS mode.
"""

from alembic import op

revision = "20261025_0020"
down_revision = "20261025_0019"
branch_labels = None
depends_on = None

POSTGRESQL_DDL = (
    (
        "CREATE OR REPLACE TRIGGER derived_raw_health_data_insert AFTER INSERT ON "
        "raw_health_data FOR EACH ROW WHEN ((NEW.data_type IN ('steps','sleep',"
        "'daily-resting-heart-rate','daily-heart-rate-variability',"
        "'daily-oxygen-saturation'))) EXECUTE FUNCTION "
        "healthos_derived_raw_health_data_insert()"
    ),
    (
        "CREATE OR REPLACE TRIGGER derived_raw_health_data_update AFTER UPDATE ON "
        "raw_health_data FOR EACH ROW WHEN ((OLD.data_type IN ('steps','sleep',"
        "'daily-resting-heart-rate','daily-heart-rate-variability',"
        "'daily-oxygen-saturation') OR NEW.data_type IN ('steps','sleep',"
        "'daily-resting-heart-rate','daily-heart-rate-variability',"
        "'daily-oxygen-saturation')) AND (NEW.raw_json IS DISTINCT FROM OLD.raw_json OR "
        "NEW.source_start_time IS DISTINCT FROM OLD.source_start_time OR "
        "NEW.source_end_time IS DISTINCT FROM OLD.source_end_time OR NEW.source_timezone "
        "IS DISTINCT FROM OLD.source_timezone OR NEW.source_deleted_at IS DISTINCT FROM "
        "OLD.source_deleted_at OR NEW.is_quarantined IS DISTINCT FROM "
        "OLD.is_quarantined)) EXECUTE FUNCTION healthos_derived_raw_health_data_update()"
    ),
    (
        "CREATE OR REPLACE TRIGGER derived_raw_health_data_supersessions_insert AFTER "
        "INSERT ON raw_health_data_supersessions FOR EACH ROW WHEN ((NEW.data_type IN "
        "('steps','sleep','daily-resting-heart-rate','daily-heart-rate-variability',"
        "'daily-oxygen-saturation'))) EXECUTE FUNCTION "
        "healthos_derived_raw_health_data_supersessions_insert()"
    ),
    (
        "CREATE OR REPLACE TRIGGER derived_raw_source_devices_insert AFTER INSERT ON "
        "raw_source_devices FOR EACH ROW WHEN ((NEW.data_type IN ('steps','sleep',"
        "'daily-resting-heart-rate','daily-heart-rate-variability',"
        "'daily-oxygen-saturation'))) EXECUTE FUNCTION "
        "healthos_derived_raw_source_devices_insert()"
    ),
    "ALTER TABLE raw_health_data ENABLE ALWAYS TRIGGER derived_raw_health_data_insert",
    "ALTER TABLE raw_health_data ENABLE ALWAYS TRIGGER derived_raw_health_data_update",
    (
        "ALTER TABLE raw_health_data_supersessions ENABLE ALWAYS TRIGGER "
        "derived_raw_health_data_supersessions_insert"
    ),
    "ALTER TABLE raw_source_devices ENABLE ALWAYS TRIGGER derived_raw_source_devices_insert",
)

PREVIOUS_POSTGRESQL_DDL = (
    (
        "CREATE OR REPLACE TRIGGER derived_raw_health_data_insert AFTER INSERT ON "
        "raw_health_data FOR EACH ROW EXECUTE FUNCTION "
        "healthos_derived_raw_health_data_insert()"
    ),
    (
        "CREATE OR REPLACE TRIGGER derived_raw_health_data_update AFTER UPDATE ON "
        "raw_health_data FOR EACH ROW WHEN (NEW.raw_json IS DISTINCT FROM OLD.raw_json "
        "OR NEW.source_start_time IS DISTINCT FROM OLD.source_start_time OR "
        "NEW.source_end_time IS DISTINCT FROM OLD.source_end_time OR NEW.source_timezone "
        "IS DISTINCT FROM OLD.source_timezone OR NEW.source_deleted_at IS DISTINCT FROM "
        "OLD.source_deleted_at OR NEW.is_quarantined IS DISTINCT FROM "
        "OLD.is_quarantined) EXECUTE FUNCTION healthos_derived_raw_health_data_update()"
    ),
    (
        "CREATE OR REPLACE TRIGGER derived_raw_health_data_supersessions_insert AFTER "
        "INSERT ON raw_health_data_supersessions FOR EACH ROW EXECUTE FUNCTION "
        "healthos_derived_raw_health_data_supersessions_insert()"
    ),
    (
        "CREATE OR REPLACE TRIGGER derived_raw_source_devices_insert AFTER INSERT ON "
        "raw_source_devices FOR EACH ROW EXECUTE FUNCTION "
        "healthos_derived_raw_source_devices_insert()"
    ),
    "ALTER TABLE raw_health_data ENABLE ALWAYS TRIGGER derived_raw_health_data_insert",
    "ALTER TABLE raw_health_data ENABLE ALWAYS TRIGGER derived_raw_health_data_update",
    (
        "ALTER TABLE raw_health_data_supersessions ENABLE ALWAYS TRIGGER "
        "derived_raw_health_data_supersessions_insert"
    ),
    "ALTER TABLE raw_source_devices ENABLE ALWAYS TRIGGER derived_raw_source_devices_insert",
)


def upgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        for statement in POSTGRESQL_DDL:
            op.execute(statement)


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        for statement in PREVIOUS_POSTGRESQL_DDL:
            op.execute(statement)
