"""Bounded, ordered reads of append-only interpretation input state."""

from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.identity import RawSourceDevice
from app.models.manual import ManualMeasurement
from app.models.manual_flags import ManualMeasurementFlagEvent
from app.services.identity_common import among

# Production reads PostgreSQL with one array. SQLite runs only the test suite, which
# drives this same function at the 50,000-record input budget: more fingerprints than
# SQLite's variable limit (999 on older builds). Leave room for the data type beside them.
_SQLITE_FINGERPRINTS = 900


async def source_decisions(
    session: AsyncSession, keys: Sequence[tuple[str, str]]
) -> list[RawSourceDevice]:
    """Read the latest decision for each exact (data type, fingerprint) pair.

    Grouping by data type keeps pairs intact while allowing `among` to use a
    PostgreSQL array. SQLite expands IN values, so only that dialect needs chunks.
    Sorted groups and chunks reproduce the original global key order, including
    missing keys. All reads use the caller's session and transaction.
    """
    groups: dict[str, list[str]] = defaultdict(list)
    for kind, fingerprint in sorted(set(keys)):
        groups[kind].append(fingerprint)
    decisions = []
    later = aliased(RawSourceDevice)
    latest = (
        ~select(later.id)
        .where(
            later.data_type == RawSourceDevice.data_type,
            later.fingerprint == RawSourceDevice.fingerprint,
            later.id > RawSourceDevice.id,
        )
        .exists()
    )
    postgres = session.get_bind().dialect.name == "postgresql"
    for kind, fingerprints in groups.items():
        block = len(fingerprints) if postgres else _SQLITE_FINGERPRINTS
        for offset in range(0, len(fingerprints), block):
            decisions.extend(
                await session.scalars(
                    select(RawSourceDevice)
                    .where(
                        RawSourceDevice.data_type == kind,
                        among(
                            session,
                            RawSourceDevice.fingerprint,
                            fingerprints[offset : offset + block],
                        ),
                        latest,
                    )
                    .order_by(RawSourceDevice.data_type, RawSourceDevice.fingerprint)
                )
            )
    return decisions


async def measurement_flags(session: AsyncSession, measurement_window, record_limit: int):
    """Read event history with one global order and one overflow sentinel.

    The caller has already checked that every measurement in this window fits
    its input budget. Selecting those same ids in SQL needs no per-id parameters,
    works for an empty window, and preserves marked measurements in the history.
    Do not filter to effective flags: mark/unmark must also change the fingerprint.
    """
    return (
        await session.execute(
            select(ManualMeasurementFlagEvent.id, ManualMeasurementFlagEvent.action)
            .where(
                ManualMeasurementFlagEvent.measurement_id.in_(
                    select(ManualMeasurement.id).where(*measurement_window)
                )
            )
            .order_by(ManualMeasurementFlagEvent.id)
            .limit(record_limit + 1)
        )
    ).all()
