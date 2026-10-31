"""The ``analytics_*`` views as migration 20261024_0013 defines them (PostgreSQL).

They count what every reader counts (app/models/eligibility.py, ``normalized_counted``): a
normalized row that is not quarantined, whose raw record (same key) is live and not
quarantined, and which is not superseded by a counted row through a link in force (its two
sources are still one device). ``health-mirror verify`` compares the views in the database
with these definitions (app/services/identity_schema.py, ``verify_views``); the migration keeps
its own frozen copy and tests/test_eligibility.py compares the two.
"""

from __future__ import annotations

from app.models.eligibility import NORMALIZED_TABLES, normalized_counted_sql

__all__ = ["NORMALIZED_TABLES", "view_select"]


def view_select(table: str) -> str:
    """The ``SELECT`` of ``analytics_<table>``: the rows of ``table`` that
    ``normalized_counted`` counts."""
    return f"SELECT * FROM {table} WHERE {normalized_counted_sql(table)}"
