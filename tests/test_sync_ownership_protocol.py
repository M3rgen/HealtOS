"""Offline connection failures supplement, but never replace, PostgreSQL process tests."""

import asyncio
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import DBAPIError

from app.sync import ownership
from app.sync.ownership import OwnedSessions, OwnershipLost, run_ownership


@asynccontextmanager
async def transaction(value=None):
    yield value


@pytest.fixture
def protocol(monkeypatch):
    engine = Mock(spec=ownership.AsyncEngine)
    engine.dialect = SimpleNamespace(name="postgresql")
    engine.url = "postgresql+asyncpg://unused"
    connection = AsyncMock(spec=ownership.AsyncConnection)
    connection.closed = connection.invalidated = False
    connection.begin = transaction
    connection.scalar.return_value = True
    isolated = SimpleNamespace(connect=AsyncMock(return_value=connection), dispose=AsyncMock())
    create = Mock(return_value=isolated)
    monkeypatch.setattr(ownership, "create_async_engine", create)
    return SimpleNamespace(
        sessions=SimpleNamespace(kw={"bind": engine}),
        engine=engine,
        connection=connection,
        isolated=isolated,
        create=create,
    )


@pytest.mark.parametrize("ending", ["success", "body-error", "acquire-error", "cancel", "busy"])
async def test_lock_connection_is_isolated_and_discarded_on_every_exit(protocol, ending):
    connection = protocol.connection
    if ending in {"acquire-error", "cancel"}:
        connection.scalar.side_effect = (
            RuntimeError("connection failed")
            if ending == "acquire-error"
            else asyncio.CancelledError()
        )
    elif ending == "busy":
        connection.scalar.return_value = False

    async def run():
        async with run_ownership(protocol.sessions, "heart-rate", wait=False) as sessions:
            if ending == "busy":
                assert sessions is None
            else:
                assert sessions.connection is connection
                assert sessions.factory.kw["bind"] is connection
                if ending == "body-error":
                    raise RuntimeError("body failed")

    if ending in {"acquire-error", "body-error", "cancel"}:
        with pytest.raises(asyncio.CancelledError if ending == "cancel" else RuntimeError):
            await run()
    else:
        await run()
    protocol.engine.connect.assert_not_called()
    assert connection.scalar.call_args.args[1] == {"namespace": 0x48455331, "key": -2018325127}
    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()
    assert protocol.isolated.dispose.await_count >= 1


def timed_out():
    return DBAPIError("SELECT", {}, SimpleNamespace(sqlstate=ownership.LOCK_NOT_AVAILABLE))


@pytest.mark.parametrize("ending", ["acquired", "error", "cancel"])
async def test_waiter_queues_on_its_one_connection_until_acquired(protocol, monkeypatch, ending):
    connection = protocol.connection
    connection.scalar.return_value = False
    contended, blocked = asyncio.Event(), asyncio.Event()
    outcomes = [timed_out(), timed_out()]
    if ending == "error":
        outcomes.append(DBAPIError("SELECT", {}, SimpleNamespace(sqlstate="57P01")))

    async def execute(statement, *args):
        if statement is ownership.WAIT_LOCK:
            if outcomes:
                raise outcomes.pop(0)
            if ending == "cancel":
                blocked.set()
                await asyncio.Event().wait()

    async def contention():
        connection.close.assert_not_awaited()
        contended.set()

    connection.execute.side_effect = execute
    monkeypatch.setattr(ownership, "_contended", contention)

    async def run():
        async with run_ownership(protocol.sessions, "heart-rate") as sessions:
            assert sessions.connection is connection

    task = asyncio.create_task(run())
    if ending == "cancel":
        await asyncio.wait_for(blocked.wait(), 5)
        task.cancel()
    expected = {"error": DBAPIError, "cancel": asyncio.CancelledError}.get(ending)
    with pytest.raises(expected) if expected else nullcontext():
        await asyncio.wait_for(task, 5)
    assert contended.is_set()
    # Every timed-out slice requeues on the same backend; no reconnect per retry.
    protocol.isolated.connect.assert_awaited_once()
    waits = [c.args[0] for c in connection.execute.await_args_list]
    assert waits == [ownership.WAIT_SLICE, ownership.WAIT_LOCK] * 3
    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()


@pytest.mark.parametrize("loss", ["closed", "invalidated", "missing-lock", "probe-error"])
async def test_every_transaction_verifies_ownership_before_exposing_a_session(protocol, loss):
    connection = protocol.connection
    sessions = OwnedSessions(connection, {"key": -2018325127})
    session = SimpleNamespace(scalar=AsyncMock(return_value=False))
    if loss in {"closed", "invalidated"}:
        setattr(connection, loss, True)
    if loss == "probe-error":
        session.scalar.side_effect = RuntimeError("connection failed")
    sessions.factory = SimpleNamespace(begin=lambda: transaction(session))
    with pytest.raises(RuntimeError if loss == "probe-error" else OwnershipLost):
        async with sessions.begin():
            pytest.fail("lost ownership must never expose a writable session")
    assert session.scalar.await_count == (0 if loss in {"closed", "invalidated"} else 1)


@pytest.mark.parametrize("namespace", [ownership.RUN_NAMESPACE, ownership.NOTIFICATION_NAMESPACE])
async def test_page_transactions_verify_the_same_backend_lock(protocol, namespace):
    owned = OwnedSessions(protocol.connection, {"namespace": namespace, "key": -2018325127})
    session = SimpleNamespace(scalar=AsyncMock(return_value=True))
    owned.factory = SimpleNamespace(begin=lambda: transaction(session))
    for _ in range(2):
        async with owned.begin() as page_session:
            assert page_session is session
    assert session.scalar.await_count == 2
    assert session.scalar.call_args.args == (
        ownership.HELD_LOCK,
        {"namespace": namespace, "unsigned_key": 2276642169},
    )


async def test_repeated_cancellation_cannot_interrupt_lock_release(protocol):
    started, release = asyncio.Event(), asyncio.Event()

    async def invalidate():
        started.set()
        await release.wait()

    protocol.connection.invalidate.side_effect = invalidate

    async def run():
        async with run_ownership(protocol.sessions, "heart-rate"):
            pass

    task = asyncio.create_task(run())
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    asyncio.get_running_loop().call_soon(task.cancel)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    protocol.connection.close.assert_awaited_once()
