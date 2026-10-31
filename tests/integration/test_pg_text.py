"""Real VARCHAR enforcement; SQLite's permissive String columns cannot prove this."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from text_records import TEXT_FIELDS, text_point

from app.google_health.client import Page
from app.google_health.data_types import REGISTRY
from app.parsers.normalize import normalize
from app.services.storage import _upsert_normalized_many, store_page

pytestmark = pytest.mark.integration


async def test_optional_labels_fit_postgresql_without_hiding_sql_errors(pg_sessions):
    for kind, path, column in TEXT_FIELDS:
        spec = REGISTRY[kind]
        model = normalize(spec, text_point(kind, path, "valid")).model
        limit = model.__table__.c[column].type.length
        valid = text_point(kind, path, "ç" * limit)
        broken = text_point(kind, path, "ç" * (limit + 1))
        async with pg_sessions.begin() as session:
            stats = await store_page(session, spec, Page([valid, broken], None))
            assert stats.normalized == 2 and stats.normalization_skipped == 0
            query = select(model.google_data_point_id, getattr(model, column))
            labels = dict((await session.execute(query)).all())
            assert labels[valid["name"]] == "ç" * limit and labels[broken["name"]] is None
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    values = normalize(spec, broken).values
                    values[column] = "ç" * (limit + 1)
                    await _upsert_normalized_many(session, model, [values])
