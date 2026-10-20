"""The verbatim store of Google Health API responses (``raw_api_responses``).

Pages are written with their records by ``app.services.storage.store_page``; this module
keeps the rest (:class:`DatabaseResponseArchive`), checks that stored bodies are intact,
and reads them back for audits (``health-mirror responses`` / ``export-response``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.google_health.capture import CapturedResponse
from app.models.raw_versions import RawApiResponse
from app.services.storage import insert_responses


class DatabaseResponseArchive:
    """Stores responses that no page transaction does, each batch in its own transaction."""

    def __init__(self, sessions: async_sessionmaker) -> None:
        self.sessions = sessions

    async def keep(self, responses: Sequence[CapturedResponse]) -> None:
        async with self.sessions.begin() as session:
            await insert_responses(session, responses)


_PG_BODY_MISMATCH = text(
    "SELECT id FROM raw_api_responses "
    "WHERE encode(sha256(body), 'hex') <> body_sha256 OR octet_length(body) <> body_size "
    "ORDER BY id"
)
_PG_WEBHOOK_MISMATCH = text(
    "SELECT payload_hash FROM webhook_events "
    "WHERE raw_body IS NOT NULL AND encode(sha256(raw_body), 'hex') <> payload_hash"
)
_DANGLING = (
    ("raw_health_data", "data_type || '/' || google_data_point_id"),
    ("raw_health_data_versions", "'history id ' || id"),
)


async def verify_responses(session: AsyncSession) -> tuple[int, list[str]]:
    """Every stored body still has its recorded SHA-256 and length; links resolve.

    Also checks signed webhook bodies (``webhook_events.raw_body``) against their batch
    hash, which is the SHA-256 of exactly those bytes. Returns (responses checked, errors).
    """
    errors: list[str] = []
    count = await session.scalar(text("SELECT count(*) FROM raw_api_responses")) or 0
    if session.get_bind().dialect.name == "postgresql":
        for (row_id,) in (await session.execute(_PG_BODY_MISMATCH)).tuples():
            errors.append(f"stored Google response {row_id} no longer matches its SHA-256")
        webhooks = (await session.execute(_PG_WEBHOOK_MISMATCH)).scalars().all()
    else:
        rows = await session.stream(
            select(RawApiResponse.id, RawApiResponse.body, RawApiResponse.body_sha256)
            .order_by(RawApiResponse.id)
            .execution_options(yield_per=200)
        )
        async for row_id, body, expected in rows:
            if hashlib.sha256(body).hexdigest() != expected:
                errors.append(f"stored Google response {row_id} no longer matches its SHA-256")
        rows = await session.execute(
            text("SELECT payload_hash, raw_body FROM webhook_events WHERE raw_body IS NOT NULL")
        )
        webhooks = [
            digest for digest, body in rows.tuples() if hashlib.sha256(body).hexdigest() != digest
        ]
    for digest in webhooks:
        errors.append(f"signed webhook body of batch {digest} no longer matches its hash")
    for table, label in _DANGLING:
        rows = await session.execute(
            text(
                f"SELECT {label} FROM {table} t WHERE response_id IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM raw_api_responses a WHERE a.id = t.response_id)"
            )
        )
        for (key,) in rows.tuples():
            errors.append(f"{table} {key} refers to a missing Google response")
    return count, errors


def describe(response: RawApiResponse) -> dict[str, Any]:
    """Metadata of a stored response (no body), for listings and export headers."""
    return {
        "id": response.id,
        "fetched_at": response.fetched_at.isoformat(),
        "source": response.source,
        "context_id": response.context_id,
        "data_type": response.data_type,
        "method": response.method,
        "path": response.path,
        "query": response.query,
        "request_body": response.request_body.decode("utf-8", "replace")
        if response.request_body
        else None,
        "attempt": response.attempt,
        "status_code": response.status_code,
        "headers": response.headers,
        # The body is stored as it came off the wire: still compressed when this is set
        # (decode with e.g. `gzip -dc`); body_sha256/body_size are over those bytes.
        "content_encoding": (response.headers or {}).get("content-encoding"),
        "body_sha256": response.body_sha256,
        "body_size": response.body_size,
        "complete": response.complete,
        "error": response.error,
    }


async def list_responses(
    session: AsyncSession,
    *,
    data_type: str | None = None,
    context_id: str | None = None,
    since: datetime | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    columns = [column for column in RawApiResponse.__table__.c if column.name != "body"]
    query = select(*columns).order_by(RawApiResponse.id.desc()).limit(limit)
    if data_type is not None:
        query = query.where(RawApiResponse.data_type == data_type)
    if context_id is not None:
        query = query.where(RawApiResponse.context_id == context_id)
    if since is not None:
        query = query.where(RawApiResponse.fetched_at >= since)
    rows = (await session.execute(query)).mappings().all()
    return [describe(RawApiResponse(**row)) for row in rows]


async def load_response(session: AsyncSession, response_id: int) -> RawApiResponse | None:
    return await session.get(RawApiResponse, response_id)
