"""Stable v2 identities (migration 20261021_0012): keys, reasons, schema and guards (SQLite)."""

from __future__ import annotations

import importlib.util
import re
from datetime import timedelta
from pathlib import Path

import pytest
from identity_records import (
    HEART,
    NEW_NAME,
    OLD_NAME,
    PACKAGE,
    SEDENTARY,
    STEPS,
    SWIM,
    T0,
    ZONES,
    fitbit,
    heart,
    interval,
    legacy,
    phone,
    sedentary,
    steps,
    swim,
)
from sqlalchemy import delete, text, update
from sqlalchemy.exc import IntegrityError, OperationalError

from app.models import identity as identity_models
from app.models.identity import RawHealthDataIdentity, RawHealthDataSupersession
from app.parsers.identity import (
    new_record_id,
    reading,
    record_identity,
    spare_record_id,
    specific_key,
    supersession_reason,
)
from app.services.raw_history import verify_guards


def test_v2_key_ignores_display_name_package_suffix_and_civil_fields() -> None:
    old, renamed = (
        record_identity(HEART, heart(60)),
        record_identity(HEART, heart(61, name=NEW_NAME)),
    )
    assert old is not None and renamed is not None
    assert old.key == renamed.key and old.source != renamed.source
    assert old.specific_key != renamed.specific_key
    assert record_identity(HEART, heart(60, civil=True)) == old  # sampleTime civil fields
    suffixed, plain = (
        record_identity(STEPS, steps(5)),
        record_identity(STEPS, steps(5, source=phone(PACKAGE))),
    )
    assert suffixed is not None and plain is not None and suffixed.key == plain.key
    shaped, flat = record_identity(SWIM, swim(9, civil=True)), record_identity(SWIM, swim(9))
    assert shaped is not None and flat is not None and shaped.fingerprint == flat.fingerprint
    # The value is never part of the identity; the time and the stable source are.
    assert record_identity(HEART, heart(99)) == old
    assert record_identity(HEART, heart(60, at=T0 + timedelta(seconds=1))).key != old.key
    other_method = {**heart(60), "dataSource": fitbit(OLD_NAME, "PASSIVELY_MEASURED")}
    assert record_identity(HEART, other_method).key != old.key


def test_growing_intervals_key_on_their_start() -> None:
    short, longer = (
        record_identity(SEDENTARY, sedentary(15)),
        record_identity(SEDENTARY, sedentary(30)),
    )
    assert short is not None and longer is not None
    assert short.key == longer.key and short.fingerprint != longer.fingerprint
    # One source's growing interval is one row: its id leaves the end out.
    assert short.specific_key == longer.specific_key
    # Other intervals keep their end in the key.
    one = record_identity(STEPS, steps(5))
    two = record_identity(STEPS, {**steps(5), "steps": {"interval": interval(T0, 2), "count": "5"}})
    assert one is not None and two is not None and one.key != two.key


def test_keys_lead_with_the_start_and_new_ids_carry_key_and_fingerprint() -> None:
    identity = record_identity(HEART, heart(60))
    renamed = record_identity(HEART, heart(60, name=NEW_NAME))
    assert identity is not None and renamed is not None
    prefix, stamp, digest = identity.key.split(":")
    assert (prefix, stamp, len(digest)) == ("v2", "20261018T090000", 64)
    assert identity.specific_key == specific_key(identity.key, identity.source)
    _, _, own = identity.specific_key.split(":")
    assert identity.specific_key.startswith("v2s:20261018T090000:")
    # A new row's id names its key and its fingerprint: two sources never share one.
    assert new_record_id(identity.specific_key) == f"synthetic:v2s:{own}"
    assert new_record_id(identity.specific_key) != new_record_id(renamed.specific_key)
    spares = {spare_record_id(identity, attempt) for attempt in range(1, 9)}
    assert len(spares) == 8 and new_record_id(identity.specific_key) not in spares


def test_reading_is_what_was_measured() -> None:
    assert reading(HEART, heart(60)) == reading(HEART, heart(60, name=NEW_NAME, civil=True))
    assert reading(HEART, heart(60)) != reading(HEART, heart(61))
    assert reading(SWIM, swim(9)) == reading(SWIM, swim(9, civil=True))
    assert reading(SEDENTARY, sedentary(15)) == reading(SEDENTARY, sedentary(30))
    assert reading(STEPS, steps(5)) != reading(
        STEPS, {**steps(5), "steps": {"interval": interval(T0, 2), "count": "5"}}
    )


def test_records_with_a_google_id_or_no_time_have_no_v2_identity() -> None:
    assert (
        record_identity(HEART, {**heart(60), "name": "users/me/dataTypes/heart-rate/dataPoints/1"})
        is None
    )
    assert (
        record_identity(HEART, {"dataSource": fitbit(), "heartRate": {"beatsPerMinute": 60}})
        is None
    )
    assert record_identity(HEART, ["not", "a", "record"]) is None


def test_zone_discriminators_keep_records_of_one_interval_apart() -> None:
    def zone(kind):
        return {
            "dataSource": fitbit(),
            "timeInHeartRateZone": {"interval": interval(T0, 1), "heartRateZoneType": kind},
        }

    light, peak = record_identity(ZONES, zone("LIGHT")), record_identity(ZONES, zone("PEAK"))
    assert light is not None and peak is not None and light.key != peak.key


def test_supersession_reasons() -> None:
    assert supersession_reason(HEART, heart(60), heart(60, name=NEW_NAME)) == "display-name-change"
    assert (
        supersession_reason(STEPS, steps(5), steps(5, source=phone(PACKAGE)))
        == "package-suffix-change"
    )
    assert supersession_reason(SWIM, swim(9), swim(9, civil=True)) == "interval-shape-change"
    assert supersession_reason(SEDENTARY, sedentary(15), sedentary(30)) == "growing-interval"
    assert supersession_reason(HEART, heart(60), heart(60)) == "duplicate"
    moved = {
        **heart(60),
        "dataSource": {**fitbit(), "device": {"displayName": OLD_NAME, "model": "x"}},
    }
    assert supersession_reason(HEART, heart(60), moved) == "source-change"


def test_model_and_migration_ddl_are_the_same() -> None:
    path = Path("migrations/versions/20261021_0012_record_identity.py")
    spec = importlib.util.spec_from_file_location("migration_0012", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def normalize_sql(statements) -> list[str]:
        return [re.sub(r"\s+", " ", statement).strip() for statement in statements]

    assert normalize_sql(migration.POSTGRESQL_DDL) == normalize_sql(identity_models.POSTGRESQL_DDL)
    assert normalize_sql(migration.SQLITE_DDL) == normalize_sql(identity_models.SQLITE_DDL)
    assert migration.IDENTITY_TABLES == identity_models.IDENTITY_TABLES
    assert migration.FUNCTION_BODIES == identity_models.FUNCTION_BODIES
    # The analytics views verify compares with are the ones the migration creates.
    from app.services import analytics_views

    assert migration.NORMALIZED_TABLES == analytics_views.NORMALIZED_TABLES
    for table in analytics_views.NORMALIZED_TABLES:
        assert migration._view(table, current_only=True) == (
            f"CREATE OR REPLACE VIEW analytics_{table} AS {analytics_views.view_select(table)}"
        )


async def test_identity_tables_are_append_only_on_sqlite(sessions) -> None:
    old = await legacy(sessions, HEART, heart(60), T0)
    new = await legacy(sessions, HEART, heart(60, name=NEW_NAME), T0 + timedelta(days=1))
    async with sessions.begin() as session:
        session.add(
            RawHealthDataIdentity(
                data_type="heart-rate", identity_key="v2:x", raw_id=new, google_data_point_id="n"
            )
        )
        session.add(
            RawHealthDataSupersession(
                old_raw_id=old,
                new_raw_id=new,
                data_type="heart-rate",
                old_google_data_point_id="o",
                new_google_data_point_id="n",
                reason="display-name-change",
                old_fingerprint="a" * 64,
                new_fingerprint="b" * 64,
                same_reading=True,
                new_payload_hash="c" * 64,
            )
        )
    for statement in (
        "UPDATE raw_health_data_identity SET identity_key = 'x'",
        "DELETE FROM raw_health_data_identity",
        "UPDATE raw_health_data_supersessions SET reason = 'x'",
        "DELETE FROM raw_health_data_supersessions",
    ):
        with pytest.raises((IntegrityError, OperationalError), match="keeps every record"):
            async with sessions.begin() as session:
                await session.execute(text(statement))
    with pytest.raises(IntegrityError):  # a row cannot supersede itself
        async with sessions.begin() as session:
            session.add(
                RawHealthDataSupersession(
                    old_raw_id=new,
                    new_raw_id=new,
                    data_type="heart-rate",
                    old_google_data_point_id="a",
                    new_google_data_point_id="a",
                    reason="duplicate",
                    old_fingerprint="a" * 64,
                    new_fingerprint="a" * 64,
                    same_reading=True,
                    new_payload_hash="c" * 64,
                )
            )


async def test_verify_reports_a_missing_identity_guard(sessions) -> None:
    async with sessions() as session:
        assert await verify_guards(session) == []
    async with sessions.begin() as session:
        await session.execute(text("DROP TRIGGER raw_health_data_supersessions_no_delete"))
    async with sessions() as session:
        assert await verify_guards(session) == [
            "protective trigger missing: "
            "raw_health_data_supersessions.raw_health_data_supersessions_no_delete"
        ]


async def test_sightings_only_widen_on_sqlite(sessions) -> None:
    from app.models.identity import RawSourceSighting

    async with sessions.begin() as session:
        session.add(
            RawSourceSighting(
                data_type="heart-rate",
                fingerprint="f" * 64,
                stable_class="s" * 64,
                label="x",
                first_seen_at=T0,
                last_seen_at=T0 + timedelta(hours=1),
                complete=True,
            )
        )
    table = RawSourceSighting.__table__
    async with sessions.begin() as session:
        await session.execute(
            update(table).values(
                first_seen_at=T0 - timedelta(days=1), last_seen_at=T0 + timedelta(days=1)
            )
        )
    for statement in (
        update(table).values(last_seen_at=T0),
        update(table).values(first_seen_at=T0),
        update(table).values(complete=False),
        update(table).values(fingerprint="g"),
        delete(table),
    ):
        with pytest.raises((IntegrityError, OperationalError), match="only widens|keeps every"):
            async with sessions.begin() as session:
                await session.execute(statement)
