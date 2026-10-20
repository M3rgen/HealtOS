"""The freshness probe must be answerable from ix_raw_health_data_type_freshness (0007)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text

from app.core.config import Settings
from app.services.pipeline_health import PipelineHealthService

pytestmark = pytest.mark.integration

INDEX = "ix_raw_health_data_type_freshness"
BASE = datetime(2026, 1, 1, tzinfo=UTC)
ROWS = 4000


async def test_freshness_probe_plan_uses_expression_index(pg_sessions) -> None:
    async with pg_sessions.begin() as session:
        # Several streams, some rows with only a start time so coalesce() matters.
        await session.execute(
            text(
                "INSERT INTO raw_health_data (id, google_data_point_id, data_type, "
                "source_start_time, source_end_time, raw_json, payload_hash, first_seen_at, "
                "last_seen_at, is_quarantined, created_at, updated_at) "
                "SELECT gen_random_uuid(), 'p' || n, "
                "(ARRAY['steps', 'heart-rate', 'sleep', 'weight'])[1 + n % 4], "
                "timestamptz '2026-01-01T00:00:00Z' + n * interval '1 minute', "
                "CASE WHEN n % 3 = 0 THEN NULL "
                "ELSE timestamptz '2026-01-01T00:01:00Z' + n * interval '1 minute' END, "
                "'{}'::json, md5(n::text) || md5(n::text), now(), now(), false, now(), now() "
                "FROM generate_series(1, :rows) AS n"
            ),
            {"rows": ROWS},
        )
        await session.execute(text("ANALYZE raw_health_data"))

    service = PipelineHealthService(
        Settings(_env_file=None, app_env="test"), pg_sessions, {"steps", "heart-rate"}
    )
    engine = pg_sessions.kw["bind"].sync_engine
    captured: list[tuple[str, object]] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "coalesce" in statement:
            captured.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        summary = await service.refresh_freshness(now=datetime(2026, 2, 1, tzinfo=UTC))
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    # The probe still answers correctly, falling back to the start time when needed.
    def newest(stream_index: int) -> datetime:
        return max(
            BASE + timedelta(minutes=n + (0 if n % 3 == 0 else 1))
            for n in range(1, ROWS + 1)
            if n % 4 == stream_index
        )

    assert summary.data_freshness == {"steps": newest(0), "heart-rate": newest(1)}
    assert len(captured) == 1
    statement, parameters = captured[0]

    async with pg_sessions() as session:
        connection = await session.connection()
        # Small tables favour a sequential scan; only usability of the index is asserted.
        await connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
        rows = await connection.exec_driver_sql(f"EXPLAIN {statement}", parameters)
        plan = "\n".join(row[0] for row in rows)
    assert plan.count(f"Index Scan Backward using {INDEX}") == 2, plan
    assert "Sort" not in plan, plan
