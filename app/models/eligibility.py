"""What every reader counts: one definition for the dashboard (UI, CSV, history), the workout
list, Umay's archive tool (daily, inventory, records), verify and the ``analytics_*`` views.

A record counts exactly when its raw row is live (not marked deleted at the source), neither the
raw row nor its normalized row (if any) is quarantined, and it is not superseded, through a link
in force, by a row that is neither marked deleted nor quarantined (``held_elsewhere``,
app/models/identity_queries.py, whose supersession logic this reuses as it is). A normalized
row without a raw row never counts; a quarantine on either side hides the record from every
reader. The views' SQL (``normalized_counted_sql``) is frozen in migration 20261024_0013;
tests/test_eligibility.py compares the two.
"""

from __future__ import annotations

from sqlalchemy import and_, or_, select

import app.models.health  # noqa: F401 - the normalized tables raw_counted probes
from app.db.base import Base
from app.models.identity_constants import SUPERSESSION_TABLE
from app.models.identity_queries import held_elsewhere, in_force_sql, key_held_elsewhere
from app.models.state import RawHealthData

# Normalized table -> the raw data types it is normalized from (app/parsers/normalize.py;
# scalar_metrics carries its own data_type: every type no other table takes).
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
_OWN_TABLE_TYPES = frozenset(name for types in NORMALIZED_TABLES.values() for name in types)


def normalized_quarantined(raw, data_types) -> list:
    """``EXISTS`` clauses: the normalized row of ``raw`` (same key) is quarantined; only the
    tables ``data_types`` (when given) are normalized into are probed."""
    clauses = []
    for name, types in NORMALIZED_TABLES.items():
        if data_types is not None and not (
            set(types) & set(data_types) if types else set(data_types) - _OWN_TABLE_TYPES
        ):
            continue
        table = Base.metadata.tables[name]
        kind = raw.data_type.in_(types) if types else table.c.data_type == raw.data_type
        clauses.append(
            select(table.c.id)
            .where(
                kind,
                table.c.google_data_point_id == raw.google_data_point_id,
                table.c.is_quarantined.is_(True),
            )
            .exists()
        )
    return clauses


def raw_counted(raw=RawHealthData, data_types=None):
    """The raw row ``raw`` (the class or an alias) counts: live, not quarantined, its
    normalized row not quarantined either, and not ``held_elsewhere``. ``data_types``, when
    the query reads only these types, limits the normalized tables probed."""
    return and_(
        raw.source_deleted_at.is_(None),
        raw.is_quarantined.is_(False),
        ~held_elsewhere(raw.id),
        *(~clause for clause in normalized_quarantined(raw, data_types)),
    )


def normalized_counted(model, data_types=None):
    """The normalized row of ``model`` counts: not quarantined, its raw row (one per key, of
    one of ``data_types``; default: the types its table is normalized from) is not quarantined
    and not marked deleted at the source, and that record is not ``key_held_elsewhere``."""
    types = NORMALIZED_TABLES[model.__tablename__] if data_types is None else data_types
    raw = select(RawHealthData.id).where(
        RawHealthData.data_type.in_(types) if types else RawHealthData.data_type == model.data_type,
        RawHealthData.google_data_point_id == model.google_data_point_id,
    )
    held = (
        [key_held_elsewhere(name, model.google_data_point_id) for name in types]
        if types
        else [key_held_elsewhere(model.data_type, model.google_data_point_id)]
    )
    return and_(
        model.is_quarantined.is_(False),
        raw.where(RawHealthData.is_quarantined.is_(False)).exists(),
        ~raw.where(RawHealthData.source_deleted_at.is_not(None)).exists(),
        ~or_(*held),
    )


def normalized_counted_sql(table: str) -> str:
    """``normalized_counted`` as SQL text, for the normalized ``table`` (the analytics views):
    index probes on the raw table's (data_type, google_data_point_id) key, its partial index
    of source-deleted rows and the supersessions' (old_google_data_point_id, data_type) index;
    the fingerprints joined now are computed once per query."""
    types = NORMALIZED_TABLES[table]
    if types:
        names = ", ".join(f"'{name}'" for name in types)
        raw_type, link_type = f"r.data_type IN ({names})", f"s.data_type IN ({names})"
    else:
        raw_type, link_type = (f"{alias}.data_type = {table}.data_type" for alias in "rs")
    key = f"WHERE {raw_type} AND r.google_data_point_id = {table}.google_data_point_id"
    return (
        "is_quarantined = false "
        f"AND EXISTS (SELECT 1 FROM raw_health_data r {key} AND r.is_quarantined = false) "
        f"AND NOT EXISTS (SELECT 1 FROM raw_health_data r {key} "
        "AND r.source_deleted_at IS NOT NULL) "
        f"AND NOT EXISTS (SELECT 1 FROM {SUPERSESSION_TABLE} s "
        "JOIN raw_health_data h ON h.id = s.new_raw_id "
        f"WHERE {link_type} AND s.old_google_data_point_id = {table}.google_data_point_id "
        f"AND h.source_deleted_at IS NULL AND h.is_quarantined = false AND {in_force_sql('s')})"
    )
