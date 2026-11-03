"""Rows archived under a hollow Google name (``.../dataPoints/``) before each record got its own.

Google returns every ``respiratory-rate-sleep-summary`` record under one name whose data point
id is empty. Until each record was identified by the name and its own time
(app/parsers/common.py, ``source_id``), every sleep was written onto the one row of that name:
the row holds the last sleep it received, and the sleeps before it are its versions.

That row is never written again, deleted or rewritten (raw data is preserved, see
CONTRIBUTING.md). When the row of the same sleep under its own id holds exactly its payload, the
old row is superseded by it (a link of one source fingerprint, always in force): every reader
then counts the sleep once, and a complete fetch that returns the sleep does not mark the old
row deleted (app/sync/deletions.py, held rows). With no such row (Google no longer returns that
sleep, or returns another value of it) the old row counts on its own, as before.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.google_health.data_types import DataTypeSpec
from app.models.state import RawHealthData
from app.parsers.common import google_name, hollow_name, source_id
from app.parsers.identity import source_fingerprint
from app.services.identity_common import PlannedLink, among, rows_by_key
from app.services.identity_writes import add_links

REASON = "hollow-name"


def holds_hollow_row(spec: DataTypeSpec, old_json: object, new_json: object) -> bool:
    """A supersession from ``old_json`` to ``new_json`` is a hollow-name link: the old row's
    name is hollow, the holder has the same name, and both payloads identify one record (the
    old one's own id, as a record is identified now, is the holder's)."""
    name = google_name(old_json)
    return (
        hollow_name(name)
        and google_name(new_json) == name
        and source_id(spec, old_json) == source_id(spec, new_json) != name
    )


async def link_hollow_rows(
    session: AsyncSession, spec: DataTypeSpec, written: dict[str, str], now: datetime
) -> int:
    """Link each row of a bare hollow name to the row ``written`` (google id -> payload hash,
    this page's last payload of each id) that holds its record with the same payload."""
    bare = {
        name
        for google_id in written
        if (name := google_id.partition("@")[0]) != google_id and hollow_name(name)
    }
    if not bare:
        return 0
    rows = await session.execute(
        select(
            RawHealthData.google_data_point_id, RawHealthData.raw_json, RawHealthData.payload_hash
        ).where(
            RawHealthData.data_type == spec.name,
            among(session, RawHealthData.google_data_point_id, sorted(bare)),
        )
    )
    links = []
    for name, raw_json, digest in rows.tuples():
        own = source_id(spec, raw_json)
        if own != name and written.get(own) == digest:
            source = source_fingerprint(raw_json.get("dataSource"))
            links.append(PlannedLink(name, own, REASON, source, source, digest))
    if not links:
        return 0
    ids = await rows_by_key(
        session,
        spec.name,
        {
            name
            for link in links
            for name in (link.old_google_data_point_id, link.new_google_data_point_id)
        },
    )
    return await add_links(session, spec.name, links, ids, now)
