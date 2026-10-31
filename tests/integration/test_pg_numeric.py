"""Compare preparation with real PostgreSQL Numeric rounding and page transactions."""

from decimal import Decimal, localcontext

import pytest
from numeric_records import BAD_NUMBERS, NUMERIC_FIELDS, numeric_point
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.google_health.data_types import REGISTRY
from app.models.health import Weight
from app.models.raw_versions import RawHealthDataVersion
from app.models.state import RawHealthData
from app.parsers.normalize import normalize
from app.services.storage import store_records

pytestmark = pytest.mark.integration


async def test_numeric_preparation_matches_postgres_and_keeps_whole_pages(pg_sessions):
    received = []
    for kind, path, column, multiplier in NUMERIC_FIELDS:
        spec = REGISTRY[kind]
        model = normalize(spec, numeric_point(kind, path, "0")).model
        precision, scale = (
            getattr(model.__table__.c[column].type, key) for key in ("precision", "scale")
        )
        with localcontext() as context:
            context.prec = 80
            quantum = Decimal(10) ** -scale
            limit = Decimal(10) ** (precision - scale)
            good = limit - quantum * Decimal("0.51")
            overflow = limit - quantum / 2
            valid = numeric_point(kind, path, str(good * Decimal(str(multiplier))))
            invalid = [
                numeric_point(kind, path, value)
                for value in (*BAD_NUMBERS, str(overflow * Decimal(str(multiplier))))
            ]
            records = [valid, *invalid]
        cast_value = text(f"SELECT CAST(CAST(:value AS TEXT) AS NUMERIC({precision}, {scale}))")
        async with pg_sessions.begin() as session:
            for sign in (1, -1):
                expected = await session.scalar(cast_value, {"value": str(good * sign)})
                parsed = normalize(
                    spec, numeric_point(kind, path, str(good * sign * Decimal(str(multiplier))))
                )
                assert parsed.values[column] == expected
                with pytest.raises(DBAPIError):
                    async with session.begin_nested():
                        await session.execute(cast_value, {"value": str(overflow * sign)})
            stats = await store_records(session, spec, records)
            # An optional column's overflow is stored as NULL; its record is kept.
            kept = [record for record in invalid if normalize(spec, record) is not None]
            assert (invalid[-1] in kept) == model.__table__.c[column].nullable
            assert stats.inserted == len(records) and stats.normalized == 1 + len(kept)
            assert stats.normalization_skipped == len(invalid) - len(kept)
        received.extend(records)
    async with pg_sessions() as session:
        stored = list(await session.scalars(select(RawHealthData.raw_json)))
        assert len(stored) == len(received) and all(record in stored for record in received)


async def test_invalid_numeric_correction_retains_versions_and_last_valid_row(pg_sessions):
    valid = numeric_point()
    broken = {**numeric_point(value="sNaN"), "name": valid["name"]}
    for record, skipped in [(valid, 0), (broken, 1), (valid, 0)]:
        async with pg_sessions.begin() as session:
            stats = await store_records(session, REGISTRY["weight"], [record])
        assert (stats.normalized, stats.normalization_skipped) == (1 - skipped, skipped)
        async with pg_sessions() as session:
            assert await session.scalar(select(Weight.kilograms)) == Decimal("72.5")
            assert await session.scalar(select(RawHealthData.raw_json)) == record
    async with pg_sessions() as session:
        versions = await session.scalars(
            select(RawHealthDataVersion).order_by(RawHealthDataVersion.version)
        )
        assert [row.raw_json for row in versions] == [valid, broken]
        assert await session.scalar(select(func.count(RawHealthData.id))) == 1
