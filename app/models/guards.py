"""Every protective trigger of the archive and its evidence tables.

Each of those revisions defines its own triggers in its model module (raw_versions, identity_guards,
manual_flags, retrieval, webhook_status, raw_rejections); this joins them for
``health-mirror verify`` (app/services/raw_history_verification.py).
"""

from __future__ import annotations

from app.models import (
    derived_invalidation,
    identity_guards,
    manual_flags,
    raw_rejections,
    raw_versions,
    retrieval,
    webhook_status,
)

_MODULES = (
    raw_versions,
    identity_guards,
    manual_flags,
    retrieval,
    derived_invalidation,
    webhook_status,
    raw_rejections,
)

# trigger -> (table, CREATE TRIGGER statement), including 0020's daily type filters.
PG_TRIGGER_SQL = {name: sql for module in _MODULES for name, sql in module.PG_TRIGGER_SQL.items()}
# (table, trigger, function) on PostgreSQL.
PROTECTIVE_TRIGGERS = tuple(item for module in _MODULES for item in module.PROTECTIVE_TRIGGERS)
# trigger -> (table, full SQL) on SQLite.
SQLITE_TRIGGER_SQL = {
    name: sql for module in _MODULES for name, sql in module.SQLITE_TRIGGER_SQL.items()
}
# function -> body of every protective PostgreSQL function.
FUNCTION_BODIES = {
    name: body for module in _MODULES for name, body in module.FUNCTION_BODIES.items()
}
