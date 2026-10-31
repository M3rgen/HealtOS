"""Flags on manual measurements, as append-only events (migration 20261024_0014).

``manual_measurements`` is append-only (migration 20261020_0011), so a flag can never be a
column set on the measurement: each decision is a row of ``manual_measurement_flag_events``
instead. ``mark`` sets a flag on one measurement, ``unmark`` clears it; the latest event per
measurement and flag (``created_at``, then ``id``) is its state. The measurement itself is
never changed, and every decision stays as history.

A measurement flagged ``test`` (an entry made to try the form) is left out of everything that
reads measurements as values (the profile card, the 3D body, Umay's daily data); the
measurement list still shows it, with its flag, so it can be cleared again.

There is no foreign key to ``manual_measurements``: its rows are never deleted (its trigger),
so the key would only guard inserts, which the API checks itself, and on PostgreSQL a key would
turn its protective ``TRUNCATE`` refusal into a foreign-key error.

The table refuses ``UPDATE``, ``DELETE`` and ``TRUNCATE`` (``ENABLE ALWAYS`` triggers calling
0011's ``healthos_refuse_change()``); ``health-mirror verify`` checks them like the ones of
0011. The trigger SQL is duplicated, frozen, in the migration; tests/test_manual_flags.py
compares the two.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DDL,
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Uuid,
    event,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Its after_create DDL defines healthos_refuse_change(), which the triggers below call.
import app.models.raw_versions  # noqa: E402, F401  isort: skip

FLAG_TABLE = "manual_measurement_flag_events"
# The append-only tables of this revision.
FLAG_TABLES = (FLAG_TABLE,)
# A test entry: left out of the profile, the 3D body and Umay's data.
TEST_FLAG = "test"
# The flags that leave a measurement out of what is computed from measurements.
EXCLUDING_FLAGS = (TEST_FLAG,)
FLAGS = EXCLUDING_FLAGS
MARK = "mark"
UNMARK = "unmark"
ACTIONS = (MARK, UNMARK)
REASON_LENGTH = 200


class ManualMeasurementFlagEvent(Base):
    __tablename__ = FLAG_TABLE
    __table_args__ = (
        PrimaryKeyConstraint("id"),
        CheckConstraint("action IN ('mark', 'unmark')", name="action"),
        # The latest event per measurement and flag is its state.
        Index("ix_manual_measurement_flag_events_current", "measurement_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    # manual_measurements.id (checked by the API; that table never loses a row).
    measurement_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    # FLAGS; a new flag needs no schema change.
    flag: Mapped[str] = mapped_column(String(32), nullable=False)
    # ACTIONS.
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(REASON_LENGTH))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# trigger -> (table, timing/events/level, function)
_PG_TRIGGERS = {}
for _table in FLAG_TABLES:
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

PROTECTIVE_TRIGGERS = tuple(
    (table, trigger, function) for trigger, (table, _, function) in _PG_TRIGGERS.items()
)
PG_TRIGGER_SQL = {
    trigger: (table, f"CREATE TRIGGER {trigger}\n{definition}\nEXECUTE FUNCTION {function}()")
    for trigger, (table, definition, function) in _PG_TRIGGERS.items()
}
# No function of its own: healthos_refuse_change() belongs to 0011 (and is checked with it).
FUNCTION_BODIES: dict[str, str] = {}

_SQLITE_TRIGGERS = {}
for _table in FLAG_TABLES:
    for _operation in ("UPDATE", "DELETE"):
        _name = f"{_table}_no_{_operation.lower()}"
        _SQLITE_TRIGGERS[_name] = (
            _table,
            f"""CREATE TRIGGER {_name}
BEFORE {_operation} ON {_table}
BEGIN
    SELECT RAISE(ABORT, '{_table} keeps every record: {_operation} refused');
END""",
        )
SQLITE_TRIGGER_SQL = dict(_SQLITE_TRIGGERS)

POSTGRESQL_DDL = (
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
