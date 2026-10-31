"""Append-only, value-free webhook diagnostic observations.

Guards refuse UPDATE/DELETE and PostgreSQL TRUNCATE, including replica sessions.
Migration 20261025_0019 freezes the same SQL for existing databases.
"""

from datetime import datetime

from sqlalchemy import DDL, DateTime, Index, Integer, String, event
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Register the shared refusal function before this module's trigger listeners.
import app.models.raw_versions  # noqa: E402, F401  isort: skip

TABLE = "webhook_status_events"


class WebhookStatusEvent(Base):
    __tablename__ = TABLE
    __table_args__ = (Index("ix_webhook_status_reason_time", "reason", "occurred_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reason: Mapped[str] = mapped_column(String(40))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


PG_TRIGGER_SQL = {}
PROTECTIVE_TRIGGERS = []
for _suffix, _events, _level in (
    ("append_only", "UPDATE OR DELETE", "ROW"),
    ("no_truncate", "TRUNCATE", "STATEMENT"),
):
    _name = f"{TABLE}_{_suffix}"
    PG_TRIGGER_SQL[_name] = (
        TABLE,
        f"CREATE TRIGGER {_name}\nBEFORE {_events} ON {TABLE} FOR EACH {_level}\n"
        "EXECUTE FUNCTION healthos_refuse_change()",
    )
    PROTECTIVE_TRIGGERS.append((TABLE, _name, "healthos_refuse_change"))

SQLITE_TRIGGER_SQL = {}
for _operation in ("UPDATE", "DELETE"):
    _name = f"{TABLE}_no_{_operation.lower()}"
    SQLITE_TRIGGER_SQL[_name] = (
        TABLE,
        f"CREATE TRIGGER {_name}\nBEFORE {_operation} ON {TABLE}\nBEGIN\n"
        f"    SELECT RAISE(ABORT, '{TABLE} keeps every record: {_operation} refused');\nEND",
    )
# healthos_refuse_change() belongs to 0011 and is verified with its other guards.
FUNCTION_BODIES: dict[str, str] = {}
POSTGRESQL_DDL = (
    *(sql for _, sql in PG_TRIGGER_SQL.values()),
    *(
        f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {name}"
        for table, name, _ in PROTECTIVE_TRIGGERS
    ),
)
SQLITE_DDL = tuple(sql for _, sql in SQLITE_TRIGGER_SQL.values())
for _dialect, _statements in (("postgresql", POSTGRESQL_DDL), ("sqlite", SQLITE_DDL)):
    for _statement in _statements:
        event.listen(Base.metadata, "after_create", DDL(_statement).execute_if(dialect=_dialect))
