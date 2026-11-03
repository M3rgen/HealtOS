"""Keep Google records jsonb cannot hold apart, so one of them never blocks its data type.

``reject_unstorable`` runs before hashing, identity resolution or any jsonb write, in the
page's own transaction: a page that rolls back leaves no rejection behind, and a retried
store adds none twice. Nothing is sanitized and nothing already stored is touched; the
record's bytes stay in its ``raw_api_responses`` row and in ``element_json``.

Health (``app/services/pipeline_health.py``) reports records that are rejected and not yet
acknowledged. ``acknowledge`` appends the maintainer's acknowledgement; a record rejected
again later (the same data type and ``element_sha256``) stays acknowledged.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence

from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.logging import event
from app.core.provenance import current_context_id
from app.google_health.data_types import DataTypeSpec
from app.google_health.jsonb import jsonb_storable
from app.models.raw_rejections import (
    REASON,
    RawHealthDataRejection,
    RawRejectionAck,
)

logger = logging.getLogger(__name__)
Rejection = RawHealthDataRejection


def rejected_count(records: Sequence[object]) -> int:
    return sum(not jsonb_storable(record) for record in records)


def element_bytes(record: object) -> bytes:
    """ASCII escapes keep U+0000, lone surrogates and NaN/Infinity exactly (never replaced)."""
    return json.dumps(record, ensure_ascii=True).encode("ascii")


async def reject_unstorable(
    session: AsyncSession,
    spec: DataTypeSpec,
    records: Sequence[object],
    *,
    source: str,
    response_id: int | None,
) -> tuple[list[object], int]:
    """Return the records jsonb can hold, in order, and how many were set apart."""
    from app.services.storage import dialect_insert

    accepted: list[object] = []
    rejected: list[dict] = []
    context_id = current_context_id()
    for ordinal, record in enumerate(records):
        if jsonb_storable(record):
            accepted.append(record)
            continue
        element = element_bytes(record)
        rejected.append(
            {
                "data_type": spec.name,
                "source": source,
                "context_id": context_id,
                "response_id": response_id,
                "ordinal": ordinal,
                "reason": REASON,
                "element_sha256": hashlib.sha256(element).hexdigest(),
                "element_json": element,
            }
        )
    if rejected:
        # Either unique key (exchange + place, or record + place without an exchange).
        statement = dialect_insert(session, Rejection.__table__).on_conflict_do_nothing()
        await session.execute(statement, rejected)
        # Counts only: the record's values never reach the log.
        event(logger, "RECORDS_REJECTED", data_type=spec.name, count=len(rejected), reason=REASON)
    return accepted, len(rejected)


async def acknowledged_through(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.max(RawRejectionAck.through_id))) or 0)


def _unacknowledged(through: int):
    """Records rejected after ``through`` that no acknowledged rejection already showed."""
    earlier = aliased(Rejection)
    return (
        select(Rejection.data_type, Rejection.element_sha256)
        .where(
            Rejection.id > through,
            ~exists().where(
                and_(
                    earlier.id <= through,
                    earlier.data_type == Rejection.data_type,
                    earlier.element_sha256 == Rejection.element_sha256,
                )
            ),
        )
        .distinct()
        .subquery()
    )


async def summary(session: AsyncSession) -> dict[str, int]:
    """Distinct rejected records awaiting acknowledgement, and the ids around them."""
    through = await acknowledged_through(session)
    pending = await session.scalar(select(func.count()).select_from(_unacknowledged(through)))
    latest = await session.scalar(select(func.max(Rejection.id)))
    return {
        "unacknowledged": int(pending or 0),
        "latest_id": int(latest or 0),
        "acknowledged_through": through,
    }


async def list_rejections(session: AsyncSession, *, limit: int) -> dict:
    """Metadata only, newest first; the record itself is read with ``--element``."""
    through = await acknowledged_through(session)
    rows = (
        await session.scalars(select(Rejection).order_by(Rejection.id.desc()).limit(limit))
    ).all()
    return {
        **(await summary(session)),
        "rejections": [
            {
                "id": row.id,
                "data_type": row.data_type,
                "source": row.source,
                "context_id": row.context_id,
                "response_id": row.response_id,
                "ordinal": row.ordinal,
                "reason": row.reason,
                "element_sha256": row.element_sha256,
                "element_size": len(row.element_json),
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "acknowledged": row.id <= through,
            }
            for row in rows
        ],
    }


async def load_element(session: AsyncSession, rejection_id: int) -> bytes | None:
    row = await session.get(Rejection, rejection_id)
    return row.element_json if row is not None else None


class UnknownRejection(LookupError):
    """No rejected record has this id."""


async def acknowledge(session: AsyncSession, through_id: int) -> tuple[int, bool]:
    """Append an acknowledgement up to ``through_id``; returns (in force, whether written).

    An id at or below the acknowledgement in force writes nothing: acknowledgements only
    move forward.
    """
    if await session.get(Rejection, through_id) is None:
        raise UnknownRejection(through_id)
    current = await acknowledged_through(session)
    if through_id <= current:
        return current, False
    session.add(RawRejectionAck(through_id=through_id))
    await session.flush()
    return through_id, True
