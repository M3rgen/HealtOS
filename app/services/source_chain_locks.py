"""Shared PostgreSQL locks for identity writes, deletion checks and reconciliation."""

from __future__ import annotations

import asyncio
import re
import zlib
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.timeutil import as_utc

# pg_advisory locks: (namespace, key). Per data type: (DATA_TYPE_LOCK, crc32(name)).
DATA_TYPE_LOCK = 0x48454931  # "HEI1"
BACKFILL_LOCK = (0x48454932, 1)
# Per data type: its deferred-deletion bookkeeping (app/sync/deletions.py).
DEFERRAL_LOCK = 0x48454933  # "HEI3"
# How long a reconciler batch tries for the exclusive lock without queueing (writers never
# wait behind it meanwhile), then how long it waits in the lock queue (every new writer of the
# data type waits behind it then), before it backs off and retries; once it holds the lock, how
# long it waits for any row lock (a long transaction of another writer). ``app.services.
# source_chains`` re-exports them and stays where they are read, on each call, so a caller
# that replaces ``source_chains.QUEUE_TIMEOUT`` or ``ROW_LOCK_TIMEOUT`` still changes the wait.
LOCK_TIMEOUT = "100ms"
QUEUE_TIMEOUT = "1s"
ROW_LOCK_TIMEOUT = "2s"


class BackfillRunning(RuntimeError):
    """Another ``identity-backfill`` holds the single-run lock."""


class LockBusy(RuntimeError):
    """The data type's exclusive lock could not be had in time (nothing was written)."""


def _key(data_type: str) -> int:
    # Same value on every process and server version (hashtext is not guaranteed stable).
    return zlib.crc32(data_type.encode()) - 2**31


def seconds(value: str) -> float:
    """A PostgreSQL duration like ``10s``, ``500ms`` or ``2min`` in seconds."""
    found = re.fullmatch(r"([0-9]+)(ms|s|min)?", value)
    if found is None:
        raise ValueError(f"not a duration: {value!r}")
    number, unit = int(found.group(1)), found.group(2) or "ms"
    return number * {"ms": 0.001, "s": 1, "min": 60}[unit]


async def try_lock_deferrals(session: AsyncSession, data_type: str) -> bool:
    """Transaction lock on ``data_type``'s deferred-deletion rows, taken only when free (always
    granted off PostgreSQL). Every writer of those rows holds it, so its holder never waits for
    a row lock there; ``False``: another transaction is writing them right now."""
    if session.get_bind().dialect.name != "postgresql":
        return True
    arguments = {"namespace": DEFERRAL_LOCK, "key": _key(data_type)}
    return bool(
        await session.scalar(text("SELECT pg_try_advisory_xact_lock(:namespace, :key)"), arguments)
    )


async def lock_data_type(
    session: AsyncSession, data_type: str, *, exclusive: bool, wait: str | None = None
) -> None:
    """Transaction lock on ``data_type``'s identities: shared for writers of records and
    deletion marks, exclusive for a reconciler batch (PostgreSQL only).

    ``wait`` (exclusive): try for the lock without queueing for this long - a request waiting
    in the queue would make every new writer of the data type wait behind it - then queue
    for at most ``QUEUE_TIMEOUT``; ``LockBusy`` when it is still held. Once the exclusive lock
    is held, every row lock is waited for at most ``ROW_LOCK_TIMEOUT``."""
    if session.get_bind().dialect.name != "postgresql":
        return
    arguments = {"namespace": DATA_TYPE_LOCK, "key": _key(data_type)}
    if not exclusive:
        await session.execute(
            text("SELECT pg_advisory_xact_lock_shared(:namespace, :key)"), arguments
        )
        return
    if wait is not None:
        loop = asyncio.get_running_loop()
        deadline, pause = loop.time() + seconds(wait), 0.01
        while True:
            if await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:namespace, :key)"), arguments
            ):
                break
            if loop.time() >= deadline:
                await _queue_for_lock(session, arguments)
                break
            await asyncio.sleep(pause)
            pause = min(pause * 2, 0.05)
    else:
        await session.execute(text("SELECT pg_advisory_xact_lock(:namespace, :key)"), arguments)
    await session.execute(text(f"SET LOCAL lock_timeout = '{_timeouts().ROW_LOCK_TIMEOUT}'"))


def _timeouts():
    """The module whose lock timeouts are in force: ``source_chains`` (imported here, on the
    call, so importing this module alone still loads neither it nor storage)."""
    from app.services import source_chains

    return source_chains


async def _queue_for_lock(session: AsyncSession, arguments: dict) -> None:
    from sqlalchemy.exc import DBAPIError

    await session.execute(text(f"SET LOCAL lock_timeout = '{_timeouts().QUEUE_TIMEOUT}'"))
    try:
        async with session.begin_nested():
            await session.execute(text("SELECT pg_advisory_xact_lock(:namespace, :key)"), arguments)
    except DBAPIError as exc:
        original = getattr(exc, "orig", None)
        code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        if code != "55P03":
            raise
        raise LockBusy("the data type's exclusive lock is held by other writers") from exc


# What the holder of the single-run lock is doing (its connection's application_name).
LOCK_PURPOSES = {
    "identity-backfill": "an identity-backfill run (the nightly reconciler, or one by hand)",
    "identity-decide": "another identity-decide (applying an owner decision)",
}


async def _lock_holder(connection) -> str:
    """Who holds the single-run lock, for the refusal message."""
    row = (
        await connection.execute(
            text(
                "SELECT a.application_name, a.backend_start FROM pg_locks l "
                "JOIN pg_stat_activity a ON a.pid = l.pid WHERE l.locktype = 'advisory' "
                "AND l.granted AND l.classid = :namespace AND l.objid = :key"
            ),
            {"namespace": BACKFILL_LOCK[0], "key": BACKFILL_LOCK[1]},
        )
    ).first()
    if row is None:
        return "another identity-backfill or identity-decide"
    name, started = row
    purpose = str(name or "").removeprefix("healthos-")
    what = LOCK_PURPOSES.get(purpose, f"a session ({name or 'unnamed'})")
    return f"{what}, connected since {as_utc(started).strftime('%Y-%m-%d %H:%M:%S')} UTC"


@asynccontextmanager
async def backfill_lock(
    sessions: async_sessionmaker,
    *,
    dry_run: bool = False,
    purpose: str = "identity-backfill",
    wait: float = 0.0,
):
    """Hold the single-run lock of identity-backfill and identity-decide (a session lock on
    its own connection, outside any transaction, named ``healthos-<purpose>``). It is tried
    for every half second for ``wait`` seconds (a decision waits briefly for a reconciler
    batch run to end), then refused, naming who holds it. A dry run writes nothing and takes
    no lock."""
    bind = sessions.kw.get("bind")
    if dry_run or bind is None or bind.dialect.name != "postgresql":
        yield
        return
    connection = await bind.connect()
    try:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(
            text("SELECT set_config('application_name', :name, false)"),
            {"name": f"healthos-{purpose}"},
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while True:
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:namespace, :key)"),
                {"namespace": BACKFILL_LOCK[0], "key": BACKFILL_LOCK[1]},
            )
            if acquired or loop.time() >= deadline:
                break
            await asyncio.sleep(min(0.5, max(0.0, deadline - loop.time())))
        if not acquired:
            holder = await _lock_holder(connection)
            waited = f" (waited {wait:g} s)" if wait else ""
            raise BackfillRunning(
                f"another identity-backfill is running: {holder} holds the single-run lock "
                f"(PostgreSQL advisory lock {BACKFILL_LOCK[0]}/{BACKFILL_LOCK[1]}){waited}; "
                f"nothing was written. Run the {purpose} again when it has finished"
                + (
                    " (the nightly reconciler takes minutes; `health-mirror status` shows "
                    "its last run)"
                    if purpose == "identity-decide"
                    else ""
                )
            )
        try:
            yield
        finally:
            await connection.execute(
                text("SELECT pg_advisory_unlock(:namespace, :key)"),
                {"namespace": BACKFILL_LOCK[0], "key": BACKFILL_LOCK[1]},
            )
    finally:
        await connection.close()
