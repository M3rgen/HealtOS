"""Flags on manual measurements: read their state, record a decision (insert only).

The state of a flag on a measurement is the latest ``manual_measurement_flag_events`` row for
the two (``created_at``, then ``id``): flagged when it is ``mark``. Readers that compute from
measurements leave out every measurement :func:`excluded_measurements` names; this is the one
place that decides it.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy import Select, and_, exists, inspect, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.manual_flags import (
    EXCLUDING_FLAGS,
    FLAG_TABLE,
    MARK,
    UNMARK,
    ManualMeasurementFlagEvent,
)


def _current(flags: Iterable[str]) -> Select:
    """The latest event per measurement and flag, for ``flags``."""
    event, later = ManualMeasurementFlagEvent, aliased(ManualMeasurementFlagEvent)
    newer = exists().where(
        later.measurement_id == event.measurement_id,
        later.flag == event.flag,
        or_(
            later.created_at > event.created_at,
            and_(later.created_at == event.created_at, later.id > event.id),
        ),
    )
    return select(event.measurement_id, event.flag, event.action).where(
        event.flag.in_(tuple(flags)), ~newer
    )


def excluded_measurements() -> Select:
    """Subquery: ids of the manual measurements whose latest event of an excluding flag
    (``test``) is ``mark``. Use as ``~ManualMeasurement.id.in_(excluded_measurements())``."""
    current = _current(EXCLUDING_FLAGS).subquery()
    return select(current.c.measurement_id).where(current.c.action == MARK).distinct()


async def excluded_measurement_ids(session: AsyncSession) -> set[uuid.UUID]:
    """The ids of the manual measurements left out as excluded (flagged ``test``)."""
    return set((await session.scalars(excluded_measurements())).all())


async def active_flags(
    session: AsyncSession, measurement_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """measurement id -> the flags currently set on it (sorted), for ``measurement_ids``."""
    ids = list(measurement_ids)
    found: dict[uuid.UUID, list[str]] = defaultdict(list)
    if not ids:
        return found
    current = _current(EXCLUDING_FLAGS).subquery()
    rows = await session.execute(
        select(current.c.measurement_id, current.c.flag)
        .where(current.c.action == MARK, current.c.measurement_id.in_(ids))
        .order_by(current.c.flag)
    )
    for measurement_id, flag in rows.tuples():
        found[measurement_id].append(flag)
    return found


async def record_flag(
    session: AsyncSession,
    measurement_id: uuid.UUID,
    flag: str,
    flagged: bool,
    reason: str | None = None,
) -> bool:
    """Set (``flagged``) or clear ``flag`` on a measurement by appending an event; the
    measurement itself is never changed. Returns False, appending nothing, when the flag is
    already in that state. The caller checks that the measurement exists and commits."""
    current = _current((flag,)).where(ManualMeasurementFlagEvent.measurement_id == measurement_id)
    action = (await session.execute(current)).tuples().first()
    is_flagged = action is not None and action[2] == MARK
    if is_flagged == flagged:
        return False
    session.add(
        ManualMeasurementFlagEvent(
            measurement_id=measurement_id,
            flag=flag,
            action=MARK if flagged else UNMARK,
            reason=reason,
        )
    )
    await session.flush()
    return True


async def flag_schema_errors(session: AsyncSession) -> list[str]:
    """``health-mirror verify`` against a database not upgraded to 20261024_0014 yet: names
    its revision and the upgrade (checking the flag table's guards would fail on SQL)."""

    def missing_at(sync) -> str | None:
        inspector = inspect(sync.connection())
        if inspector.has_table(FLAG_TABLE):
            return None
        if not inspector.has_table("alembic_version"):
            return "unknown"
        return sync.scalar(text("SELECT version_num FROM alembic_version")) or "unknown"

    revision = await session.run_sync(missing_at)
    if revision is None:
        return []
    return [
        f"the database is at revision {revision}, without the manual measurement flags of "
        f"20261024_0014 ({FLAG_TABLE}): run `alembic upgrade head` (docs/RUNBOOK.md, "
        "20261024_0014) before verifying with this version"
    ]
