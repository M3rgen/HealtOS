"""Run the archive regressions against actual PostgreSQL VARCHAR bindings."""

import pytest
from test_webhook_route import (
    test_metadata_column_boundaries_are_accepted as check_boundaries,
)
from test_webhook_route import (
    test_unusable_metadata_is_rejected_without_rolling_back_batch as check_rejection,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "field,column,limit",
    [
        ("dataType", "data_type", 128),
        ("healthUserId", "health_user_id", 128),
        ("operation", "operation", 32),
    ],
)
@pytest.mark.parametrize("bad", [{}, [], "ğ" * 129])
@pytest.mark.parametrize("shape", ["object", "array", "mixed"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_postgres_rejects_bad_metadata_independently(
    pg_sessions, field, column, limit, bad, shape, legacy
):
    await check_rejection(pg_sessions, field, column, limit, bad, shape, legacy)


async def test_postgres_accepts_exact_character_limits(pg_sessions):
    await check_boundaries(pg_sessions)
