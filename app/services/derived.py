"""Bounded read-model rebuilds; dirty results are never returned as current."""

import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select, text, update
from sqlalchemy.exc import InterfaceError, OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.derived import DerivedDay
from app.models.derived_invalidation import BUILD_LOCK
from app.models.identity import plan_each_query
from app.services.canonical_daily import METHOD, bounds, canonical_day
from app.services.storage import dialect_insert

logger = logging.getLogger(__name__)
MAX_RANGE_DAYS = 366
REFRESH_BATCH = 16
BUILD_FAILED = "derived_build_failed"


class RetryReport(TypedDict):
    """Day counts only. processed = succeeded + failed + stale.

    skipped counts days needing no retry: no failed row, or one another builder
    repaired before the fence. A stale day was rebuilt while its source changed;
    the refresh queue builds it again, so it is not a failure.
    """

    candidates: int
    processed: int
    succeeded: int
    failed: int
    stale: int
    skipped: int


def _key(day, timezone):
    return (DerivedDay.day == day, DerivedDay.timezone == timezone, DerivedDay.method == METHOD)


def _check(first: date, last: date) -> int:
    days = (last - first).days + 1
    if not 1 <= days <= MAX_RANGE_DAYS:
        raise ValueError("Türetilmiş veri aralığı 1–366 gün olmalı.")
    return days


def _failure(exc: BaseException) -> dict:
    # Stored in place of a result: the build of THIS revision failed. Only the exception
    # type is kept, never its message (it may quote source values).
    return {"build_error": {"reason_codes": [BUILD_FAILED], "error_type": type(exc).__name__}}


def build_failed(result: object) -> bool:
    return isinstance(result, dict) and "build_error" in result


def _error(day: date, revision: int | None = None) -> dict:
    item = {"date": day.isoformat(), "state": "error", "result": None}
    if revision is not None:
        item["revision"] = revision
    return item | {"reason_codes": [BUILD_FAILED]}


async def register_range(session: AsyncSession, first: date, last: date, timezone: str) -> None:
    """Persist requested work before a bounded worker runs; no archive-wide discovery."""
    days = _check(first, last)
    rows = []
    for offset in range(days):
        day = first + timedelta(days=offset)
        start, stop = bounds(day, timezone)
        rows.append(
            dict(
                day=day,
                timezone=timezone,
                method=METHOD,
                window_start=start,
                window_stop=stop,
                civil_start=datetime.combine(day, time.min, UTC),
                civil_stop=datetime.combine(day + timedelta(days=1), time.min, UTC),
            )
        )
    await session.execute(
        dialect_insert(session, DerivedDay.__table__).values(rows).on_conflict_do_nothing()
    )


async def rebuild_day(
    session: AsyncSession, day: date, timezone: str, *, retry_failed: bool = False
) -> dict:
    # Lock BEFORE creating a missing day or reading any inputs. Writers acquire the shared
    # lock inside their triggers. They invalidate after this transaction commits, including
    # when the day did not exist when the writer started. No source row lock is taken here.
    if session.get_bind().dialect.name == "postgresql":
        # The fence needs statement snapshots taken after the lock. An older REPEATABLE
        # READ snapshot could miss a committed write and persist the day as current.
        isolation = await session.scalar(text("SELECT current_setting('transaction_isolation')"))
        if isolation != "read committed":
            row = (
                await session.execute(
                    select(DerivedDay.revision, DerivedDay.built_revision, DerivedDay.result).where(
                        *_key(day, timezone)
                    )
                )
            ).first()
            if row is not None and row[0] == row[1] and build_failed(row[2]):
                return _error(day, row[0])
            return {"date": day.isoformat(), "state": "stale", "result": None}
        await session.execute(text(f"SELECT pg_advisory_xact_lock({BUILD_LOCK})"))
    if not retry_failed:
        await register_range(session, day, day, timezone)
    await plan_each_query(session)
    row = (
        await session.execute(
            select(DerivedDay).where(*_key(day, timezone)).execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    # Recheck AFTER the fence: another builder may have repaired this candidate while
    # we waited. Maintenance never creates cold days or overwrites a healthy result.
    if retry_failed and (row is None or not build_failed(row.result)):
        return {"date": day.isoformat(), "state": "skipped", "result": None}
    assert row is not None
    revision = row.revision
    try:
        result = await canonical_day(session, day, timezone)
    except SQLAlchemyError:
        raise  # the transaction itself failed; nothing is recorded for this revision
    except Exception as exc:
        # Isolated: this revision is recorded as failed instead of blocking its range
        # and the refresh queue. Any later source change makes it pending again.
        logger.warning("Derived day build failed: %s", type(exc).__name__)
        result = _failure(exc)
    await session.execute(
        update(DerivedDay)
        .where(*_key(day, timezone))
        .values(
            result=result,
            built_revision=revision,
            computed_at=datetime.now(UTC),
            build_count=DerivedDay.build_count + 1,
        )
        .execution_options(synchronize_session=False)
    )
    # A mutation in this transaction during calculation must remain pending as well.
    current = (
        await session.execute(select(DerivedDay.revision).where(*_key(day, timezone)))
    ).scalar_one()
    if current != revision:
        return {"date": day.isoformat(), "state": "stale", "result": None}
    if build_failed(result):
        return _error(day, revision)
    return {"date": day.isoformat(), "state": "current", "revision": revision, "result": result}


async def _record_pending_failure(
    session: AsyncSession, day: date, timezone: str, revision: int, exc: Exception
) -> None:
    """Record a rolled-back build's failure only if its work is still pending.

    The key and source revision must still match, and either the built revision must
    lag or the result must be absent, just as in the refresh queue. A completed result
    (including an already recorded error) owns its timestamp and build count.
    """
    # Rollback released the build lock. Another builder may have completed this SAME
    # revision before this transaction; revision alone cannot identify pending work.
    # Keep eligibility in the UPDATE so checking and recording are one atomic write.
    await session.execute(
        update(DerivedDay)
        .where(
            *_key(day, timezone),
            DerivedDay.revision == revision,
            or_(
                DerivedDay.built_revision != DerivedDay.revision,
                DerivedDay.result.is_(None),
            ),
        )
        .values(
            result=_failure(exc),
            built_revision=revision,
            computed_at=datetime.now(UTC),
            build_count=DerivedDay.build_count + 1,
        )
        .execution_options(synchronize_session=False)
    )


async def build_day(sessions, day: date, timezone: str, revision: int | None = None) -> dict:
    """One day in its own short transaction: the build lock covers this day only.

    A failure stays with its day. ``revision`` is the committed revision the caller saw;
    a non-transient database error is then recorded only while that revision is pending.
    A source write in between leaves the new revision pending; a successful builder's
    committed result is preserved. Transient errors stay unrecorded and pending.
    """
    try:
        async with sessions() as session, session.begin():
            return await rebuild_day(session, day, timezone)
    except Exception as exc:
        logger.warning("Derived day build failed: %s", type(exc).__name__)
        if revision is not None and not isinstance(exc, (OperationalError, InterfaceError)):
            try:
                async with sessions() as session, session.begin():
                    await _record_pending_failure(session, day, timezone, revision, exc)
            except Exception as again:
                logger.warning("Derived day failure not recorded: %s", type(again).__name__)
        return _error(day)


async def build_range(sessions, first: date, last: date, timezone: str) -> int:
    """Build cold or pending days BEFORE a range is read, one transaction per day.

    Ingestion then waits for one day's build at most, never for a whole range, and the
    reader's own transaction (possibly REPEATABLE READ) builds nothing. A revision that
    already failed is not retried here; a later source change makes it pending again.
    Days are registered (committed) first, so every failure has a revision to record.
    Safe without the lock: a writer either bumps the committed row or commits before
    the day's build acquires the lock and reads its inputs.
    """
    async with sessions() as session, session.begin():
        await register_range(session, first, last, timezone)  # also bounds the range
        rows = (
            await session.execute(
                select(
                    DerivedDay.day,
                    DerivedDay.revision,
                    DerivedDay.built_revision,
                    DerivedDay.result.is_(None),
                ).where(
                    DerivedDay.day >= first,
                    DerivedDay.day <= last,
                    DerivedDay.timezone == timezone,
                    DerivedDay.method == METHOD,
                )
            )
        ).all()
    count = 0
    for day, revision, built, empty in sorted(rows):
        if revision != built or empty:
            await build_day(sessions, day, timezone, revision)
            count += 1
    return count


def check_retry_range(first: date, last: date, timezone: str) -> int:
    """Validate the whole maintenance request before opening any database session."""
    days = _check(first, last)
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Geçerli bir IANA saat dilimi gerekli.") from exc
    return days


async def retry_failed_range(
    sessions, first: date, last: date, timezone: str, *, dry_run: bool = False
) -> RetryReport:
    """Retry only existing build_error rows of METHOD in an explicit bounded window.

    Each day commits independently. Database failures roll back to the existing error
    marker; no unlocked fallback write can overwrite a concurrent successful build.
    Cancellation propagates, rolling back only the active day. Pending failed results
    are eligible too: the fenced build reads their latest source revision.
    """
    days = check_retry_range(first, last, timezone)
    async with sessions() as session:
        rows = (
            await session.execute(
                select(DerivedDay.day, DerivedDay.result)
                .where(
                    DerivedDay.day >= first,
                    DerivedDay.day <= last,
                    DerivedDay.timezone == timezone,
                    DerivedDay.method == METHOD,
                )
                .order_by(DerivedDay.day)
            )
        ).all()
    candidates = [day for day, result in rows if build_failed(result)]
    report = RetryReport(
        candidates=len(candidates),
        processed=0,
        succeeded=0,
        failed=0,
        stale=0,
        skipped=days - len(candidates),
    )
    if dry_run:
        return report
    for day in candidates:
        try:
            async with sessions() as session, session.begin():
                item = await rebuild_day(session, day, timezone, retry_failed=True)
        except Exception as exc:
            logger.warning("Derived day retry failed: %s", type(exc).__name__)
            item = _error(day)
        state = item["state"]
        if state == "skipped":
            report["skipped"] += 1
            continue
        report["processed"] += 1
        report[
            "succeeded" if state == "current" else "stale" if state == "stale" else "failed"
        ] += 1
    return report


async def derived_range(session: AsyncSession, first: date, last: date, timezone: str) -> dict:
    days = _check(first, last)
    rows = (
        (
            await session.execute(
                select(DerivedDay)
                .where(
                    DerivedDay.day >= first,
                    DerivedDay.day <= last,
                    DerivedDay.timezone == timezone,
                    DerivedDay.method == METHOD,
                )
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    cached = {row.day: row for row in rows}
    results = []
    for offset in range(days):
        day = first + timedelta(days=offset)
        row = cached.get(day)
        if row is not None and row.revision == row.built_revision and build_failed(row.result):
            results.append(_error(day, row.revision))
        elif row is not None and row.result is not None and row.revision == row.built_revision:
            results.append(
                {
                    "date": day.isoformat(),
                    "state": "current",
                    "revision": row.revision,
                    "result": row.result,
                }
            )
        else:
            results.append(await rebuild_day(session, day, timezone))
    # Recheck ALL revisions after builds: a change during a build may invalidate another
    # cached day of this response. Source writes outside this transaction are fenced.
    revisions = (
        await session.execute(
            select(DerivedDay.day, DerivedDay.revision, DerivedDay.built_revision).where(
                DerivedDay.day >= first,
                DerivedDay.day <= last,
                DerivedDay.timezone == timezone,
                DerivedDay.method == METHOD,
            )
        )
    ).all()
    versions = {day: (revision, built) for day, revision, built in revisions}
    for item in results:
        if versions.get(date.fromisoformat(item["date"])) != (item.get("revision"),) * 2:
            item.update(state="stale", result=None)
            item.pop("reason_codes", None)
    return {
        "schema_version": "1",
        "method_version": METHOD,
        "timezone": timezone,
        "source_selection": "none",
        "days": results,
    }


class DerivedRefresh:
    def __init__(self, sessions):
        self.sessions = sessions

    async def run(self) -> int:
        """At most sixteen registered days per run, one transaction each, restart-safe.

        Cold days are seeded by explicit range requests, never by scanning the archive.
        After an interruption/rollback the row stays pending; revision differences ARE the
        queue. A day whose build fails is recorded as failed for its revision and skipped,
        so it cannot hold the queue; a later source change makes it pending again.
        """
        async with self.sessions() as session:
            keys = (
                await session.execute(
                    select(DerivedDay.day, DerivedDay.timezone, DerivedDay.revision)
                    .where(
                        DerivedDay.method == METHOD,
                        or_(
                            DerivedDay.revision != DerivedDay.built_revision,
                            DerivedDay.result.is_(None),
                        ),
                    )
                    .order_by(DerivedDay.day, DerivedDay.timezone)
                    .limit(REFRESH_BATCH)
                )
            ).all()
        count = 0
        for day, timezone, revision in keys:
            await build_day(self.sessions, day, timezone, revision)
            count += 1
        return count
