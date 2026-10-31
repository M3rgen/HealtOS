"""SQL predicates for device membership and live supersession holders."""

from __future__ import annotations

from sqlalchemy import and_, or_, select, text, tuple_
from sqlalchemy.orm import aliased

from app.models.identity_constants import DEVICE_TABLE
from app.models.identity_tables import RawHealthDataSupersession, RawSourceDevice
from app.models.state import RawHealthData


def joined_pairs():
    """``SELECT`` of every (data type, fingerprint, fingerprint) that are one device now (the
    latest ``raw_source_devices`` row of each, alike). Uncorrelated, so a reader computes it
    once (a hashed subplan that keeps parallel plans possible), however many rows it checks."""
    device, later = aliased(RawSourceDevice), aliased(RawSourceDevice)
    current = (
        select(device.data_type, device.fingerprint, device.device)
        .where(
            ~select(later.id)
            .where(
                later.data_type == device.data_type,
                later.fingerprint == device.fingerprint,
                later.id > device.id,
            )
            .exists()
        )
        .subquery()
    )
    one, other = current.alias(), current.alias()
    return select(one.c.data_type, one.c.fingerprint, other.c.fingerprint).join(
        other, and_(other.c.data_type == one.c.data_type, other.c.device == one.c.device)
    )


def in_force(link=RawHealthDataSupersession):
    """The supersession ``link`` (the class or an alias) is in force: its two fingerprints
    are one, or one device now. A link whose fingerprints are no longer one device (a
    rejected or refuted chain, two devices decided by the owner) hides nothing; one whose
    fingerprints never had a device logged is not in force either."""
    return or_(
        link.old_fingerprint == link.new_fingerprint,
        tuple_(link.data_type, link.old_fingerprint, link.new_fingerprint).in_(joined_pairs()),
    )


def in_force_sql(link: str) -> str:
    """``in_force`` as SQL text, for the supersession alias ``link`` (the analytics views)."""
    current = (
        f"(SELECT d.data_type, d.fingerprint, d.device FROM {DEVICE_TABLE} d WHERE NOT EXISTS "
        f"(SELECT 1 FROM {DEVICE_TABLE} e WHERE e.data_type = d.data_type "
        "AND e.fingerprint = d.fingerprint AND e.id > d.id))"
    )
    return (
        f"({link}.old_fingerprint = {link}.new_fingerprint OR ({link}.data_type, "
        f"{link}.old_fingerprint, {link}.new_fingerprint) IN (SELECT a.data_type, a.fingerprint, "
        f"b.fingerprint FROM {current} a JOIN {current} b ON b.data_type = a.data_type "
        "AND b.device = a.device))"
    )


def superseded(raw_id):
    """``EXISTS`` clause: the raw row ``raw_id`` is superseded by another row (an in-force
    link, whatever the holder's mark)."""
    link = aliased(RawHealthDataSupersession)
    return select(link.old_raw_id).where(link.old_raw_id == raw_id, in_force(link)).exists()


def held_elsewhere(raw_id):
    """``EXISTS`` clause: ``raw_id`` is superseded, by an in-force link, by a row that is
    neither marked deleted at the source nor quarantined. Readers skip exactly these rows (the
    holder counts their record); a superseded row whose holders were all marked deleted (or
    quarantined) counts again when it is live itself, so a link can never hide a live record
    behind a row that is not counted, and a link no longer in force hides nothing. The write
    path decides with the very same test (app/services/identity.py, ``_Page.hiders``)."""
    holder = aliased(RawHealthData)
    link = aliased(RawHealthDataSupersession)
    return (
        select(link.old_raw_id)
        .join(holder, holder.id == link.new_raw_id)
        .where(
            link.old_raw_id == raw_id,
            holder.source_deleted_at.is_(None),
            holder.is_quarantined.is_(False),
            in_force(link),
        )
        .exists()
    )


async def plan_each_query(session) -> None:
    """Plan every reader statement of this transaction with its own parameters (PostgreSQL):
    a generic plan, which a long-lived connection switches to after five executions, cannot
    use the bounds of ``in_force`` and of the time range, and reads many times more rows."""
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text("SET LOCAL plan_cache_mode = force_custom_plan"))


def key_held_elsewhere(data_type, google_data_point_id):
    """``held_elsewhere`` of the raw row with this data type and ``google_data_point_id``,
    found through the supersessions' ``(old_google_data_point_id, data_type)`` index: for a
    normalized row (keyed like its raw row), so a reader probes the links of the rows it reads
    instead of joining every raw row of the data type (as the ``analytics_*`` views do)."""
    holder = aliased(RawHealthData)
    link = aliased(RawHealthDataSupersession)
    return (
        select(link.old_raw_id)
        .join(holder, holder.id == link.new_raw_id)
        .where(
            link.old_google_data_point_id == google_data_point_id,
            link.data_type == data_type,
            holder.source_deleted_at.is_(None),
            holder.is_quarantined.is_(False),
            in_force(link),
        )
        .exists()
    )


def holds_live(raw_id):
    """``EXISTS`` clause: ``raw_id`` holds, through an in-force link, a row that is live and
    not quarantined (a row marked deleted that a live duplicate stands for)."""
    held = aliased(RawHealthData)
    link = aliased(RawHealthDataSupersession)
    return (
        select(link.new_raw_id)
        .join(held, held.id == link.old_raw_id)
        .where(
            link.new_raw_id == raw_id,
            held.source_deleted_at.is_(None),
            held.is_quarantined.is_(False),
            in_force(link),
        )
        .exists()
    )
