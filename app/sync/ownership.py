"""Run ownership, separate from transaction-scoped identity locks.

A PostgreSQL owner uses one isolated connection for both its session advisory lock and
all short page transactions. A waiter keeps its one connection and queues in the
server's lock manager in short slices: the lock passes in request order, no new
connection is opened per retry and no snapshot is held for the whole wait. Checking the
lock inside every transaction prevents SQLAlchemy reconnecting a lost connection and
continuing to write without ownership. No transaction spans Google I/O.
"""

from __future__ import annotations

import asyncio
import fcntl
import zlib
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

RUN_NAMESPACE = 0x48455331  # "HES1"; never the identity writers' HEI namespaces.
NOTIFICATION_NAMESPACE = 0x48455731  # "HEW1"; independent of data-type run locks.
TRY_LOCK = text("SELECT pg_try_advisory_lock(:namespace, :key)")
WAIT_LOCK = text("SELECT pg_advisory_lock(:namespace, :key)")
# A slice that times out leaves the queue only for one round trip before requeueing.
WAIT_SLICE = text("SET LOCAL lock_timeout = '2s'")
LOCK_NOT_AVAILABLE = "55P03"
HELD_LOCK = text(
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
    "AND pid = pg_backend_pid() AND classid = :namespace AND objid = :unsigned_key "
    "AND objsubid = 2 AND granted AND mode = 'ExclusiveLock')"
)
MAX_BACKOFF = 2.0


class OwnershipLost(RuntimeError):
    """The run may no longer write; leave the last committed checkpoint resumable."""


async def _contended() -> None:
    """Runs once when a waiting acquisition finds the lock taken; tests observe it."""


class OwnedSessions:
    """A transaction factory bound to the lock's exact connection, never the pool."""

    def __init__(self, connection: AsyncConnection, arguments: dict):
        self.connection = connection
        self.arguments = {
            "namespace": arguments.get("namespace", RUN_NAMESPACE),
            "unsigned_key": arguments["key"] % 2**32,
        }
        self.factory = async_sessionmaker(connection, expire_on_commit=False)

    @asynccontextmanager
    async def begin(self):
        if self.connection.closed or self.connection.invalidated:
            raise OwnershipLost("sync ownership connection was lost")
        async with self.factory.begin() as session:
            if not await session.scalar(HELD_LOCK, self.arguments):
                raise OwnershipLost("sync ownership lock was lost")
            yield session


class CheckedSessions:
    """Check a caller's ownership before each short data-type transaction.

    The check finishes before the transaction starts. A webhook takes notification
    ownership first, then data-type ownership; neither recovery path takes both locks.
    In particular, a disconnected notification waiting for a data type cannot fetch.
    """

    def __init__(self, sessions, check: Callable[[], Awaitable[None]]):
        self.sessions = sessions
        self.check = check

    @asynccontextmanager
    async def begin(self):
        await self.check()
        async with self.sessions.begin() as session:
            yield session


def isolated_engine(engine: AsyncEngine) -> AsyncEngine:
    """A private pool with the driver's configured creator and connect arguments.

    It cannot starve OAuth or response archiving using the application's pool, even at
    pool_size=1. Release it with `discard_isolated` on every exit.
    """
    return create_async_engine(engine.url, pool=engine.pool.recreate())


async def discard_isolated(connection: AsyncConnection | None, engine: AsyncEngine) -> None:
    """Close the physical connection and pool even when shutdown cancels repeatedly.

    Closing the connection releases its session locks, including when acquisition or a
    commit was cancelled after PostgreSQL granted one. It never returns to a pool.
    """
    cleanup = asyncio.create_task(_close_isolated(connection, engine))
    interrupted = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            interrupted = True
    cleanup.result()
    if interrupted:
        raise asyncio.CancelledError


async def _close_isolated(connection: AsyncConnection | None, engine: AsyncEngine) -> None:
    try:
        if connection is not None:
            try:
                await connection.invalidate()
            finally:
                await connection.close()
    finally:
        await engine.dispose()


@asynccontextmanager
async def run_ownership(
    sessions: async_sessionmaker,
    data_type: str,
    *,
    wait: bool = True,
    namespace: int = RUN_NAMESPACE,
):
    """Yield owned transactions, or None for a busy nonblocking recovery attempt."""
    engine = sessions.kw["bind"]
    if not isinstance(engine, AsyncEngine):
        raise TypeError("sync ownership requires an engine-bound session factory")
    if engine.dialect.name == "sqlite":
        async with _sqlite_ownership(engine, namespace, data_type, wait) as acquired:
            yield sessions if acquired else None
        return
    if engine.dialect.name != "postgresql":
        raise ValueError("sync ownership requires PostgreSQL or SQLite")
    arguments = {"namespace": namespace, "key": zlib.crc32(data_type.encode()) - 2**31}
    isolated = isolated_engine(engine)
    connection = None
    owned = False
    try:
        connection = await isolated.connect()
        owned = await _acquire(connection, arguments, wait)
        if owned:
            yield OwnedSessions(connection, arguments)
    finally:
        await discard_isolated(connection, isolated)
    if not owned:
        yield None


async def _acquire(connection: AsyncConnection, arguments: dict, wait: bool) -> bool:
    async with connection.begin():
        if await connection.scalar(TRY_LOCK, arguments):
            return True
    if not wait:
        return False
    await _contended()
    while True:
        try:
            # Session locks survive the commit; the slice only bounds the snapshot.
            async with connection.begin():
                await connection.execute(WAIT_SLICE)
                await connection.execute(WAIT_LOCK, arguments)
            return True
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) != LOCK_NOT_AVAILABLE:
                raise


class _LocalLock:
    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0  # coroutines holding or waiting for the lock


# Entries exist only while held or awaited, so per-notification keys never accumulate.
_local_locks: dict[tuple[str, int, str], _LocalLock] = {}


@asynccontextmanager
async def _sqlite_ownership(engine: AsyncEngine, namespace: int, key: str, wait: bool):
    """Serialize in request order in-process; data types also use a flock across processes.

    SQLite has a single writer, and a notification claim is an atomic status update,
    so the notification namespace needs no sidecar file per element.
    """
    database = engine.url.database or ":memory:"
    memory = database == ":memory:"
    name = (f"memory:{id(engine)}" if memory else _resolved(database), namespace, key)
    entry = _local_locks.get(name)
    if entry is None:
        entry = _local_locks[name] = _LocalLock()
    entry.users += 1
    try:
        if entry.users > 1:
            if not wait:
                yield False
                return
            await _contended()
        async with entry.lock:
            if memory or namespace != RUN_NAMESPACE:
                yield True
                return
            async with _sqlite_file_lock(database, key, wait) as acquired:
                yield acquired
    finally:
        entry.users -= 1
        if entry.users == 0 and _local_locks.get(name) is entry:
            del _local_locks[name]


@asynccontextmanager
async def _sqlite_file_lock(database: str, data_type: str, wait: bool):
    # A sidecar remains on disk: unlinking it would let a waiter lock an old inode.
    with _sqlite_file(database, data_type) as handle:
        delay = 0.05
        contended = False
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait:
                    yield False
                    return
                if not contended:
                    contended = True
                    await _contended()
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF)
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _resolved(database: str) -> str:
    return str(Path(database).resolve())


def _sqlite_file(database: str, data_type: str):
    path = Path(database).resolve()
    key = zlib.crc32(data_type.encode())
    return path.with_name(f"{path.name}.sync-{key:08x}.lock").open("a")
