"""Append identity aliases, supersession links and their merge events."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import RawHealthDataIdentity, RawHealthDataSupersession
from app.services.identity_common import PlannedLink, among, rows_by_key


async def add_aliases(
    session: AsyncSession,
    data_type: str,
    new_aliases: dict[str, str],
    raw_ids: dict[str, uuid.UUID] | None = None,
) -> int:
    """Insert ``identity key -> row`` aliases (rows must exist); existing keys are kept.
    ``raw_ids``: google id -> raw id of the rows, when the caller knows them."""
    if not new_aliases:
        return 0
    from app.services.storage import dialect_insert

    if raw_ids is None or any(google_id not in raw_ids for google_id in new_aliases.values()):
        raw_ids = {**(raw_ids or {}), **await rows_by_key(session, data_type, new_aliases.values())}
    values = [
        {
            "data_type": data_type,
            "identity_key": key,
            "raw_id": raw_ids[google_id],
            "google_data_point_id": google_id,
        }
        for key, google_id in sorted(new_aliases.items())
        if google_id in raw_ids
    ]
    if not values:
        return 0
    statement = dialect_insert(session, RawHealthDataIdentity.__table__).on_conflict_do_nothing(
        index_elements=["data_type", "identity_key"]
    )
    for offset in range(0, len(values), 1000):
        await session.execute(statement, values[offset : offset + 1000])
    return len(values)


async def add_links(
    session: AsyncSession,
    data_type: str,
    links: Sequence[PlannedLink],
    raw_ids: dict[str, uuid.UUID],
    now: datetime,
) -> int:
    """Insert the planned links (and the superseded row's ``identity-merged`` event, once per
    row ever); ``raw_ids``: google id -> raw id of the rows written or locked."""
    from app.models.identity import MERGED_EVENT
    from app.models.raw_versions import RawHealthDataEvent
    from app.services.storage import dialect_insert

    values = []
    for link in sorted(
        links, key=lambda link: (link.old_google_data_point_id, link.new_google_data_point_id)
    ):
        old = raw_ids.get(link.old_google_data_point_id)
        new = raw_ids.get(link.new_google_data_point_id)
        if old is None or new is None or old == new:
            continue
        values.append(
            {
                "old_raw_id": old,
                "new_raw_id": new,
                "data_type": data_type,
                "old_google_data_point_id": link.old_google_data_point_id,
                "new_google_data_point_id": link.new_google_data_point_id,
                "reason": link.reason,
                "old_fingerprint": link.old_fingerprint,
                "new_fingerprint": link.new_fingerprint,
                "same_reading": True,
                "new_payload_hash": link.holder_payload_hash,
            }
        )
    if not values:
        return 0
    table = RawHealthDataSupersession.__table__
    inserted: list[uuid.UUID] = []
    for offset in range(0, len(values), 1000):
        statement = (
            dialect_insert(session, table)
            .values(values[offset : offset + 1000])
            .on_conflict_do_nothing(index_elements=["old_raw_id", "new_raw_id"])
            .returning(table.c.old_raw_id)
        )
        inserted.extend((await session.execute(statement)).scalars().all())
    names = {value["old_raw_id"]: value["old_google_data_point_id"] for value in values}
    # One identity-merged event per row, ever (a row linked before has one).
    merged = set(
        (
            await session.scalars(
                select(RawHealthDataEvent.raw_id).where(
                    RawHealthDataEvent.event == MERGED_EVENT,
                    among(session, RawHealthDataEvent.raw_id, sorted(set(inserted), key=str)),
                )
            )
        ).all()
    )
    events = [
        {
            "raw_id": raw_id,
            "data_type": data_type,
            "google_data_point_id": names[raw_id],
            "event": MERGED_EVENT,
            "occurred_at": now,
            "previous_deleted_at": None,
        }
        for raw_id in sorted(set(inserted) - merged, key=str)
    ]
    for offset in range(0, len(events), 1000):
        await session.execute(insert(RawHealthDataEvent), events[offset : offset + 1000])
    return len(inserted)
