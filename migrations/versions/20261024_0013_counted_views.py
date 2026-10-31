"""The `analytics_*` views count what every reader counts.

Until now the views (and the default dashboard) counted a normalized row unless it was
quarantined, its raw record was marked deleted at the source, or the record was held by a
counted row: a normalized row without any raw row, and one whose raw row alone was
quarantined, still counted, and a holder whose raw row alone was quarantined was counted
together with the row it superseded (that row counts again once its holder does not). The
history, the workouts and Umay's raw tools already required a live, unquarantined raw row.
Now every reader shares one definition (app/models/eligibility.py, `normalized_counted`): a
normalized row counts when it is not quarantined, a raw row with its key is live and not
quarantined, and that record is not superseded, by a link in force, by a row that is neither
marked deleted nor quarantined.

On PostgreSQL the views are replaced (`CREATE OR REPLACE`, same columns); no table, row or
index changes. Each attempt replaces all views in a savepoint, each waiting at most 100 ms
for a reader of it, and is retried until `lock_timeout` (30 s, `-x lock-timeout=...`), so
readers never queue behind the upgrade; when no attempt succeeds it fails, changing nothing.
SQLite (tests only) keeps its views, as 0012 did. downgrade() restores the views of
20261021_0012.

The view SQL below is a frozen copy of app/services/analytics_views.py (upgrade) and of
20261021_0012 (downgrade); tests/test_eligibility.py compares them.
"""

import re
import time

import sqlalchemy as sa
from alembic import context, op

revision = "20261024_0013"
down_revision = "20261021_0012"
branch_labels = None
depends_on = None

SUPERSESSION_TABLE = "raw_health_data_supersessions"
DEVICE_TABLE = "raw_source_devices"

# The normalized tables, and the raw data types each is normalized from
# (app/parsers/normalize.py; scalar_metrics carries its own data_type).
NORMALIZED_TABLES = {
    "active_zone_minutes": ("active-zone-minutes",),
    "distance": ("distance",),
    "energy_expenditure": ("active-energy-burned", "total-calories"),
    "exercise_sessions": ("exercise",),
    "heart_rate": ("heart-rate",),
    "heart_rate_variability": ("heart-rate-variability", "daily-heart-rate-variability"),
    "oxygen_saturation": ("oxygen-saturation", "daily-oxygen-saturation"),
    "respiratory_rate": ("daily-respiratory-rate", "respiratory-rate-sleep-summary"),
    "resting_heart_rate": ("daily-resting-heart-rate",),
    "scalar_metrics": (),
    "sleep_sessions": ("sleep",),
    "steps": ("steps",),
    "temperature": ("core-body-temperature", "daily-sleep-temperature-derivations"),
    "vo2_max": ("vo2-max", "daily-vo2-max", "run-vo2-max"),
    "weight": ("weight",),
}

_CURRENT = (
    f"(SELECT d.data_type, d.fingerprint, d.device FROM {DEVICE_TABLE} d WHERE NOT EXISTS "
    f"(SELECT 1 FROM {DEVICE_TABLE} e WHERE e.data_type = d.data_type "
    "AND e.fingerprint = d.fingerprint AND e.id > d.id))"
)
# A link is in force while its two fingerprints are one, or one device now (the latest
# raw_source_devices row of each alike).
_IN_FORCE = (
    "(s.old_fingerprint = s.new_fingerprint OR (s.data_type, s.old_fingerprint, "
    "s.new_fingerprint) IN (SELECT a.data_type, a.fingerprint, b.fingerprint FROM "
    f"{_CURRENT} a JOIN {_CURRENT} b ON b.data_type = a.data_type AND b.device = a.device))"
)


def _parts(table: str) -> tuple[str, str]:
    """The quarantine filter and the conditions on its raw row and links, as in 0012."""
    types = NORMALIZED_TABLES[table]
    if types:
        names = ", ".join(f"'{name}'" for name in types)
        raw_type, link_type = f"r.data_type IN ({names})", f"s.data_type IN ({names})"
    else:
        raw_type, link_type = (f"{alias}.data_type = {table}.data_type" for alias in "rs")
    key = f"WHERE {raw_type} AND r.google_data_point_id = {table}.google_data_point_id"
    deleted_or_held = (
        f"AND NOT EXISTS (SELECT 1 FROM raw_health_data r {key} "
        "AND r.source_deleted_at IS NOT NULL) "
        f"AND NOT EXISTS (SELECT 1 FROM {SUPERSESSION_TABLE} s "
        "JOIN raw_health_data h ON h.id = s.new_raw_id "
        f"WHERE {link_type} AND s.old_google_data_point_id = {table}.google_data_point_id "
        f"AND h.source_deleted_at IS NULL AND h.is_quarantined = false AND {_IN_FORCE})"
    )
    return key, deleted_or_held


def _view(table: str) -> str:
    """This revision's view: 0012's, and an unquarantined raw row with the key must exist."""
    key, deleted_or_held = _parts(table)
    return (
        f"CREATE OR REPLACE VIEW analytics_{table} AS "
        f"SELECT * FROM {table} WHERE is_quarantined = false "
        f"AND EXISTS (SELECT 1 FROM raw_health_data r {key} AND r.is_quarantined = false) "
        f"{deleted_or_held}"
    )


def _view_0012(table: str) -> str:
    """The view of 20261021_0012 (downgrade)."""
    _, deleted_or_held = _parts(table)
    return (
        f"CREATE OR REPLACE VIEW analytics_{table} AS "
        f"SELECT * FROM {table} WHERE is_quarantined = false {deleted_or_held}"
    )


LOCK_TIMEOUT = "30s"
# How long replacing one analytics view waits for a reader of it, per attempt.
VIEW_LOCK_TIMEOUT = "100ms"
_LOCK_NOT_AVAILABLE = "55P03"


def _lock_timeout() -> str:
    value = context.get_x_argument(as_dictionary=True).get("lock-timeout", LOCK_TIMEOUT)
    if not re.fullmatch(r"[1-9][0-9]*(ms|s|min)?", value):
        raise ValueError(f"lock-timeout must look like 30s, 500ms or 2min, not {value!r}")
    return value


def _seconds(value: str) -> float:
    found = re.fullmatch(r"([0-9]+)(ms|s|min)?", value)
    assert found is not None
    return int(found.group(1)) * {"ms": 0.001, "s": 1, "min": 60}[found.group(2) or "ms"]


def _replace(statements: list[str], target: str) -> None:
    """Run the view ``statements`` in one attempt (a savepoint) retried until the lock
    timeout: each view waits at most ``VIEW_LOCK_TIMEOUT`` for its readers, and a failed
    attempt releases what it took, so no reader waits for long behind the upgrade."""
    timeout = _lock_timeout()
    if context.is_offline_mode():
        op.execute(f"SET LOCAL lock_timeout = '{timeout}'")
        for statement in statements:
            op.execute(statement)
        op.execute("SET LOCAL lock_timeout TO DEFAULT")
        return
    bind = op.get_bind()
    deadline = time.monotonic() + _seconds(timeout)
    pause, attempts = 0.05, 0
    while True:
        attempts += 1
        try:
            with bind.begin_nested():
                bind.execute(sa.text(f"SET LOCAL lock_timeout = '{VIEW_LOCK_TIMEOUT}'"))
                for statement in statements:
                    bind.execute(sa.text(statement))
            return
        except sa.exc.DBAPIError as exc:
            code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
            if code != _LOCK_NOT_AVAILABLE:
                raise
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"20261024_0013 could not replace the analytics_* views within {timeout} "
                    f"({attempts} attempts): another session kept reading one. Nothing was "
                    f"changed and the database is still on {target}; retry when that session "
                    "has finished."
                ) from exc
            time.sleep(pause)
            pause = min(pause * 2, 0.5)
        finally:
            bind.execute(sa.text("SET LOCAL lock_timeout TO DEFAULT"))


def upgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    _replace([_view(table) for table in NORMALIZED_TABLES], down_revision)


def downgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    _replace([_view_0012(table) for table in NORMALIZED_TABLES], revision)
