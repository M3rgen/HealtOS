"""The ``analytics_*`` views as migration 20261021_0012 defines them (PostgreSQL).

They show what the dashboard counts: rows of a quarantined or source-deleted raw record, and
rows superseded by a live row through a link in force (its two sources are still one device),
are left out. ``health-mirror verify`` compares the views in the
database with these definitions (app/services/identity.py, ``verify_views``); the migration
keeps its own frozen copy and tests/test_identity_keys.py compares the two.
"""

from __future__ import annotations

from app.models.identity import SUPERSESSION_TABLE, in_force_sql

# Normalized table -> the raw data types it is normalized from (app/parsers/normalize.py;
# scalar_metrics carries its own data_type).
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


def view_select(table: str) -> str:
    """The ``SELECT`` of ``analytics_<table>``: like the dashboard, a normalized row counts
    unless it is quarantined, its raw record (same key) is marked deleted at the source, or
    that record is superseded, by a link in force, by a row that is not marked deleted
    (``held_elsewhere``). The lookups are index probes: the raw table's (data_type,
    google_data_point_id) key, the supersessions' (old_google_data_point_id, data_type) index
    and, for a link between two fingerprints, the devices' (data_type, fingerprint, id)."""
    types = NORMALIZED_TABLES[table]
    if types:
        names = ", ".join(f"'{name}'" for name in types)
        raw_type, link_type = f"r.data_type IN ({names})", f"s.data_type IN ({names})"
    else:
        raw_type, link_type = (f"{alias}.data_type = {table}.data_type" for alias in "rs")
    return (
        f"SELECT * FROM {table} WHERE is_quarantined = false "
        f"AND NOT EXISTS (SELECT 1 FROM raw_health_data r "
        f"WHERE {raw_type} AND r.google_data_point_id = {table}.google_data_point_id "
        "AND r.source_deleted_at IS NOT NULL) "
        f"AND NOT EXISTS (SELECT 1 FROM {SUPERSESSION_TABLE} s "
        "JOIN raw_health_data h ON h.id = s.new_raw_id "
        f"WHERE {link_type} AND s.old_google_data_point_id = {table}.google_data_point_id "
        f"AND h.source_deleted_at IS NULL AND {in_force_sql('s')})"
    )
