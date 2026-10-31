"""Read raw sightings from restored archives, including older schemas."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.timeutil import as_utc
from app.models.raw_versions import RawHealthDataVersion
from app.services.raw_history_types import _Candidate


def source_database_url(from_db: str, target_url: str) -> str:
    """A database name on the target's server, or a full SQLAlchemy URL."""
    if "://" in from_db:
        url = make_url(from_db)
        if url.drivername == "postgresql":
            url = url.set(drivername="postgresql+asyncpg")
    else:
        url = make_url(target_url).set(database=from_db)
    if url.render_as_string(hide_password=False) == target_url:
        raise ValueError("the source database is the live archive itself")
    return url.render_as_string(hide_password=False)


async def newest_sighting(engine: AsyncEngine) -> datetime | None:
    """When the dump last saw any record (orders several dumps newest first)."""
    async with engine.connect() as connection:
        value = await connection.scalar(
            text("SELECT max(last_seen_at) FROM raw_health_data").columns(
                max=DateTime(timezone=True)
            )
        )
    return as_utc(value) if isinstance(value, datetime) else None


async def _source_columns(engine: AsyncEngine) -> dict[str, set[str]]:
    def read(connection) -> dict[str, set[str]]:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        return {
            table: {column["name"] for column in inspector.get_columns(table)}
            for table in ("raw_health_data", RawHealthDataVersion.__tablename__)
            if table in tables
        }

    async with engine.connect() as connection:
        return await connection.run_sync(read)


_TIMES = {
    "source_start_time": DateTime(timezone=True),
    "source_end_time": DateTime(timezone=True),
    "received_at": DateTime(timezone=True),
    "last_seen_at": DateTime(timezone=True),
    "superseded_at": DateTime(timezone=True),
    "first_seen_at": DateTime(timezone=True),
    "source_deleted_at": DateTime(timezone=True),
}


def _source_query(table: str, columns: set[str]):
    # raw_json is read as text and parsed here: a json-typed column (dumps before
    # 20261020_0010) and SQLite text then behave the same, and a JSON string payload is
    # not mistaken for already-parsed text.
    def pick(*names: str) -> str:
        return next((name for name in names if name in columns), "NULL")

    sql = (
        "SELECT data_type, google_data_point_id, CAST(raw_json AS TEXT) AS raw_text, "
        "payload_hash, source_start_time, source_end_time, source_timezone, "
        f"{pick('received_at', 'payload_received_at')} AS received_at, last_seen_at, "
        f"{pick('superseded_at')} AS superseded_at, {pick('first_seen_at')} AS first_seen_at, "
        f"{pick('source_deleted_at')} AS source_deleted_at, "
        f"{pick('is_quarantined')} AS is_quarantined, "
        f"{pick('quarantine_reason')} AS quarantine_reason "
        f"FROM {table} ORDER BY data_type, google_data_point_id, last_seen_at"
    )
    return text(sql).columns(
        data_type=String(),
        google_data_point_id=String(),
        raw_text=String(),
        payload_hash=String(),
        source_timezone=String(),
        is_quarantined=Boolean(),
        quarantine_reason=String(),
        **_TIMES,
    )


def _candidate(row) -> _Candidate:
    return _Candidate(
        row.data_type,
        row.google_data_point_id,
        row.raw_text,
        row.payload_hash,
        as_utc(row.source_start_time),
        as_utc(row.source_end_time),
        row.source_timezone,
        as_utc(row.received_at),
        as_utc(row.last_seen_at),
        as_utc(row.superseded_at),
        as_utc(row.first_seen_at),
        as_utc(row.source_deleted_at),
        row.is_quarantined,
        row.quarantine_reason,
    )
