"""Verify the PostgreSQL identity schema and analytics view definitions."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# The constraints of migration 20261021_0012, as pg_get_constraintdef prints them.
EXPECTED_CONSTRAINTS = {
    "raw_health_data_identity": {
        "pk_raw_health_data_identity": "PRIMARY KEY (data_type, identity_key)",
        "fk_raw_health_data_identity_raw_id_raw_health_data": (
            "FOREIGN KEY (raw_id) REFERENCES raw_health_data(id)"
        ),
    },
    "raw_health_data_supersessions": {
        "pk_raw_health_data_supersessions": "PRIMARY KEY (old_raw_id, new_raw_id)",
        "ck_raw_health_data_supersessions_distinct_rows": "CHECK ((old_raw_id <> new_raw_id))",
        "fk_raw_health_data_supersessions_old_raw_id_raw_health_data": (
            "FOREIGN KEY (old_raw_id) REFERENCES raw_health_data(id)"
        ),
        "fk_raw_health_data_supersessions_new_raw_id_raw_health_data": (
            "FOREIGN KEY (new_raw_id) REFERENCES raw_health_data(id)"
        ),
    },
    "raw_source_chains": {
        "pk_raw_source_chains": "PRIMARY KEY (id)",
        "uq_raw_source_chains_link": "UNIQUE (data_type, from_fingerprint, to_fingerprint)",
        "ck_raw_source_chains_distinct_sources": (
            "CHECK (((from_fingerprint)::text <> (to_fingerprint)::text))"
        ),
    },
    "raw_source_pairs": {
        "pk_raw_source_pairs": (
            "PRIMARY KEY (data_type, first_fingerprint, second_fingerprint, identity_key)"
        ),
        "ck_raw_source_pairs_ordered": (
            "CHECK (((first_fingerprint)::text < (second_fingerprint)::text))"
        ),
    },
    "raw_source_decisions": {
        "pk_raw_source_decisions": "PRIMARY KEY (id)",
        "ck_raw_source_decisions_distinct_sources": (
            "CHECK (((first_fingerprint)::text <> (second_fingerprint)::text))"
        ),
        "ck_raw_source_decisions_known_decision": (
            "CHECK (((decision)::text = ANY ((ARRAY['link'::character varying, "
            "'reject'::character varying, 'confirm'::character varying, "
            "'distinct'::character varying, 'not-distinct'::character varying])::text[])))"
        ),
    },
    "raw_source_chain_events": {
        "pk_raw_source_chain_events": (
            "PRIMARY KEY (data_type, from_fingerprint, to_fingerprint, event)"
        ),
    },
    "raw_source_devices": {
        "pk_raw_source_devices": "PRIMARY KEY (id)",
    },
    "raw_source_sightings": {
        "pk_raw_source_sightings": "PRIMARY KEY (data_type, fingerprint)",
    },
    "raw_source_agreements": {
        "pk_raw_source_agreements": "PRIMARY KEY (id)",
        "ck_raw_source_agreements_ordered": (
            "CHECK (((first_fingerprint)::text < (second_fingerprint)::text))"
        ),
    },
}
# Keys and checks only: NOT NULL constraints (pg_constraint lists them from PostgreSQL 18 on)
# and triggers are verified elsewhere.
_CONSTRAINTS_SQL = text(
    "SELECT c.relname, k.conname, pg_get_constraintdef(k.oid), k.convalidated, k.condeferrable "
    "FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = current_schema() AND c.relname = ANY(:tables) "
    "AND k.contype IN ('p', 'u', 'c', 'f', 'x')"
)
_RULES_SQL = text(
    "SELECT tablename, rulename FROM pg_rules "
    "WHERE schemaname = current_schema() AND tablename = ANY(:tables)"
)
_VIEW_COLUMNS_SQL = text(
    "SELECT c.relname, a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = current_schema() AND c.relkind = 'v' AND c.relname = ANY(:views) "
    "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY c.relname, a.attnum"
)
# Row-level security on a guarded table can hide rows from readers and verify alike.
_ROW_SECURITY_SQL = text(
    "SELECT c.relname, c.relkind, c.relrowsecurity, c.relforcerowsecurity, "
    "(SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = current_schema() AND c.relname = ANY(:tables)"
)
_VIEWS_SQL = text(
    "SELECT c.relname, pg_get_viewdef(c.oid) FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind = 'v' AND (c.relnamespace = pg_my_temp_schema() OR "
    "(n.nspname = current_schema() AND c.relname = ANY(:views)))"
)


async def identity_tables_missing(session: AsyncSession) -> list[str]:
    """The tables of migration 20261021_0012 this database lacks (PostgreSQL)."""
    from app.models.identity import GUARDED_TABLES

    if session.get_bind().dialect.name != "postgresql":
        return []
    present = set(
        (
            await session.scalars(
                text(
                    "SELECT c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = current_schema() AND c.relname = ANY(:tables)"
                ),
                {"tables": list(GUARDED_TABLES)},
            )
        ).all()
    )
    return [table for table in GUARDED_TABLES if table not in present]


async def verify_identity_schema(session: AsyncSession, guarded: Iterable[str]) -> list[str]:
    """PostgreSQL: the identity tables keep their keys and checks (a dropped primary key
    would let a row be superseded twice, a dropped check let a row supersede itself), no
    rule sits on a guarded table (``DO INSTEAD`` would bypass the triggers), no guarded table
    has row-level security or a policy (it could hide rows), every guarded table is a plain
    table, and the ``analytics_*`` views are exactly the ones of 0013."""
    if session.get_bind().dialect.name != "postgresql":
        return []
    errors: list[str] = []
    tables = sorted(guarded)
    present = set()
    security = await session.execute(_ROW_SECURITY_SQL, {"tables": tables})
    for table, kind, enabled, forced, policies in security.tuples():
        present.add(table)
        kind = kind.decode() if isinstance(kind, bytes) else kind
        if kind != "r":
            errors.append(f"guarded table {table} is not a plain table (relkind {kind})")
        if enabled or forced or policies:
            errors.append(
                f"guarded table {table} has row-level security (enabled={enabled}, "
                f"forced={forced}, policies={policies}); it could hide rows: run ALTER TABLE "
                f"{table} NO FORCE ROW LEVEL SECURITY, DISABLE ROW LEVEL SECURITY and drop "
                "its policies"
            )
    for table in tables:
        if table not in present:
            errors.append(f"guarded table missing: {table}")
    errors.extend(await verify_views(session))
    found: dict[str, dict[str, tuple]] = defaultdict(dict)
    rows = await session.execute(_CONSTRAINTS_SQL, {"tables": sorted(EXPECTED_CONSTRAINTS)})
    for table, name, definition, validated, deferrable in rows.tuples():
        found[table][name] = (definition, validated, deferrable)
    for table, expected in EXPECTED_CONSTRAINTS.items():
        for name, definition in expected.items():
            actual = found[table].pop(name, None)
            if actual is None:
                errors.append(f"constraint missing: {table}.{name}")
            elif actual[0] != definition or not actual[1] or actual[2]:
                errors.append(f"constraint {table}.{name} was changed: {actual[0]}")
        for name, (definition, _, _) in sorted(found[table].items()):
            errors.append(f"unexpected constraint on {table}: {name} {definition}")
    for table, rule in (await session.execute(_RULES_SQL, {"tables": sorted(guarded)})).tuples():
        errors.append(f"rule {rule} on guarded table {table} (it could bypass the triggers)")
    return errors


async def verify_views(session: AsyncSession) -> list[str]:
    """The ``analytics_*`` views of 20261024_0013 were not redefined (they count what every
    reader counts, ``normalized_counted``): each is compared with what this server makes of
    the expected definition (a temporary view, in a rolled-back savepoint).

    ``SELECT *`` is expanded when a view is created, so a column added to a normalized table
    later is not in its view: the expected definition selects the view's own columns, and
    only the conditions (what is counted) must match."""
    from app.services.analytics_views import NORMALIZED_TABLES, view_select

    errors: list[str] = []
    views = [f"analytics_{table}" for table in NORMALIZED_TABLES]
    columns: dict[str, list[str]] = defaultdict(list)
    for view, column in (await session.execute(_VIEW_COLUMNS_SQL, {"views": views})).tuples():
        columns[view].append(column)
    savepoint = await session.begin_nested()
    try:
        for table in NORMALIZED_TABLES:
            expected = view_select(table)
            own = columns.get(f"analytics_{table}")
            if own:
                listed = ", ".join(f'{table}."{column}"' for column in own)
                expected = expected.replace(
                    f"SELECT * FROM {table}", f"SELECT {listed} FROM {table}", 1
                )
            try:
                async with session.begin_nested():
                    await session.execute(
                        text(f"CREATE TEMP VIEW healthos_expected_{table} AS {expected}")
                    )
            except Exception:  # noqa: BLE001 - a column the table lacks: redefined
                errors.append(
                    f"analytics view analytics_{table} was redefined: its columns are not "
                    f"columns of {table} (recreate it from migration 20261024_0013)"
                )
        found = dict((await session.execute(_VIEWS_SQL, {"views": views})).tuples().all())
    finally:
        await savepoint.rollback()
    for table in NORMALIZED_TABLES:
        actual = found.get(f"analytics_{table}")
        expected = found.get(f"healthos_expected_{table}")
        if actual is None:
            errors.append(f"analytics view missing: analytics_{table}")
        elif expected is not None and " ".join(actual.split()) != " ".join(expected.split()):
            errors.append(
                f"analytics view analytics_{table} was redefined: it no longer counts what the "
                "dashboard counts (recreate it from migration 20261024_0013)"
            )
    return errors
