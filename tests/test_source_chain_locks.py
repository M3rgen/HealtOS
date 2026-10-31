"""Lock protocol checks with synthetic sessions; PostgreSQL concurrency stays in integration."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import DBAPIError

from app.services import source_chain_locks as locks
from app.services import source_chains

DATA_TYPE = "heart-rate"
DATA_KEY = {"namespace": 0x48454931, "key": -2018325127}
RUN_KEY = {"namespace": 0x48454932, "key": 1}
TRY_XACT = "SELECT pg_try_advisory_xact_lock(:namespace, :key)"
XACT = "SELECT pg_advisory_xact_lock(:namespace, :key)"
TRY_RUN = "SELECT pg_try_advisory_lock(:namespace, :key)"
UNLOCK = "SELECT pg_advisory_unlock(:namespace, :key)"


class Session:
    def __init__(self, *, dialect="postgresql", results=(), error=None):
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect))
        self.results = iter(results)
        self.error = error
        self.calls = []

    def get_bind(self):
        return self.bind

    async def scalar(self, statement, arguments):
        self.calls.append((str(statement), arguments))
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result

    async def execute(self, statement, arguments=None):
        self.calls.append((str(statement), arguments))
        if str(statement) == XACT and self.error is not None:
            raise self.error

    @asynccontextmanager
    async def begin_nested(self):
        self.calls.append(("savepoint", None))
        try:
            yield
        except Exception:
            self.calls.append(("rollback savepoint", None))
            raise
        else:
            self.calls.append(("release savepoint", None))


class Connection:
    def __init__(self, *, results=(True,), holder=None, fail_at=None):
        self.results = iter(results)
        self.holder = holder
        self.fail_at = fail_at
        self.calls = []
        self.close = AsyncMock()

    def record(self, action, arguments):
        self.calls.append((action, arguments))
        if action == self.fail_at:
            raise RuntimeError("connection failed")

    async def execution_options(self, **options):
        self.record("options", options)
        return self

    async def execute(self, statement, arguments):
        self.record(str(statement), arguments)
        return SimpleNamespace(first=lambda: self.holder)

    async def scalar(self, statement, arguments):
        self.record(str(statement), arguments)
        return next(self.results)


def factory(connection, *, dialect="postgresql"):
    engine = SimpleNamespace(
        dialect=SimpleNamespace(name=dialect), connect=AsyncMock(return_value=connection)
    )
    return SimpleNamespace(kw={"bind": engine})


@pytest.fixture
def timer(monkeypatch):
    class Timer:
        now = 0.0

        def __init__(self):
            self.pauses = []

        def time(self):
            return self.now

        async def sleep(self, delay):
            self.pauses.append(delay)
            self.now += delay

    clock = Timer()
    monkeypatch.setattr(
        locks, "asyncio", SimpleNamespace(get_running_loop=lambda: clock, sleep=clock.sleep)
    )
    return clock


def test_legacy_exports_and_production_consumers_share_the_implementation():
    from app.services import identity, identity_backfill, identity_decisions, identity_reconciler
    from app.sync import deletions

    for name in (
        "BACKFILL_LOCK",
        "DATA_TYPE_LOCK",
        "DEFERRAL_LOCK",
        "LOCK_TIMEOUT",
        "QUEUE_TIMEOUT",
        "ROW_LOCK_TIMEOUT",
        "LOCK_PURPOSES",
        "BackfillRunning",
        "LockBusy",
        "_key",
        "seconds",
        "try_lock_deferrals",
        "lock_data_type",
        "_queue_for_lock",
        "_lock_holder",
        "backfill_lock",
    ):
        assert getattr(source_chains, name) is getattr(locks, name)
    assert identity.lock_data_type is deletions.lock_data_type is locks.lock_data_type
    assert identity_backfill.lock_data_type is locks.lock_data_type
    assert deletions.try_lock_deferrals is locks.try_lock_deferrals
    assert (
        identity_backfill.backfill_lock is identity_decisions.backfill_lock is locks.backfill_lock
    )
    assert identity_backfill.LockBusy is locks.LockBusy
    assert identity_reconciler.BackfillRunning is locks.BackfillRunning
    assert locks.lock_data_type.__module__ == locks.BackfillRunning.__module__ == locks.__name__


@pytest.mark.parametrize(
    "value, expected", [("10s", 10), ("500ms", 0.5), ("2min", 120), ("7", 0.007)]
)
def test_postgresql_durations(value, expected):
    assert locks.seconds(value) == expected


@pytest.mark.parametrize("value", ["-1s", "1.5s", "1h", "", "1s trailing"])
def test_invalid_duration(value):
    with pytest.raises(ValueError, match="not a duration"):
        locks.seconds(value)


async def test_non_postgresql_skips_transaction_locks():
    session = Session(dialect="sqlite")
    assert await locks.try_lock_deferrals(session, DATA_TYPE) is True
    await locks.lock_data_type(session, DATA_TYPE, exclusive=False)
    await locks.lock_data_type(session, DATA_TYPE, exclusive=True, wait="100ms")
    assert session.calls == []


@pytest.mark.parametrize("available", [False, True])
async def test_deferral_lock_uses_its_own_namespace(available):
    session = Session(results=[available])
    assert await locks.try_lock_deferrals(session, DATA_TYPE) is available
    assert session.calls == [(TRY_XACT, {"namespace": 0x48454933, "key": DATA_KEY["key"]})]


async def test_shared_lock_keeps_the_key_and_does_not_set_a_timeout():
    session = Session()
    await locks.lock_data_type(session, DATA_TYPE, exclusive=False)
    assert session.calls == [("SELECT pg_advisory_xact_lock_shared(:namespace, :key)", DATA_KEY)]


async def test_exclusive_lock_without_polling_sets_row_timeout_after_acquisition():
    session = Session()
    await locks.lock_data_type(session, DATA_TYPE, exclusive=True)
    assert session.calls == [(XACT, DATA_KEY), ("SET LOCAL lock_timeout = '2s'", None)]


async def test_exclusive_polling_succeeds_without_queueing(timer):
    session = Session(results=[False, False, True])
    await locks.lock_data_type(session, DATA_TYPE, exclusive=True, wait="100ms")
    assert timer.pauses == [0.01, 0.02]
    assert session.calls == [(TRY_XACT, DATA_KEY)] * 3 + [("SET LOCAL lock_timeout = '2s'", None)]


async def test_row_lock_timeout_replaced_on_the_legacy_module_is_in_force(monkeypatch):
    # PostgreSQL reconciler tests shorten the wait through ``source_chains``, as before the split.
    monkeypatch.setattr(source_chains, "ROW_LOCK_TIMEOUT", "300ms")
    session = Session()
    await locks.lock_data_type(session, DATA_TYPE, exclusive=True)
    assert session.calls == [(XACT, DATA_KEY), ("SET LOCAL lock_timeout = '300ms'", None)]


async def test_queue_timeout_replaced_on_the_legacy_module_is_in_force(monkeypatch, timer):
    monkeypatch.setattr(source_chains, "QUEUE_TIMEOUT", "250ms")
    monkeypatch.setattr(source_chains, "ROW_LOCK_TIMEOUT", "300ms")
    session = Session(results=[False])
    await locks.lock_data_type(session, DATA_TYPE, exclusive=True, wait="0ms")
    assert session.calls == [
        (TRY_XACT, DATA_KEY),
        ("SET LOCAL lock_timeout = '250ms'", None),
        ("savepoint", None),
        (XACT, DATA_KEY),
        ("release savepoint", None),
        ("SET LOCAL lock_timeout = '300ms'", None),
    ]


def queued_calls(ending="release savepoint"):
    return [
        ("SET LOCAL lock_timeout = '1s'", None),
        ("savepoint", None),
        (XACT, DATA_KEY),
        (ending, None),
    ]


async def test_exclusive_lock_queues_only_after_polling_deadline(timer):
    session = Session(results=[False] * 5)
    await locks.lock_data_type(session, DATA_TYPE, exclusive=True, wait="120ms")
    assert timer.pauses == [0.01, 0.02, 0.04, 0.05]
    assert session.calls == (
        [(TRY_XACT, DATA_KEY)] * 5 + queued_calls() + [("SET LOCAL lock_timeout = '2s'", None)]
    )


def database_error(attribute, code):
    original = RuntimeError("database error")
    setattr(original, attribute, code)
    return DBAPIError(XACT, DATA_KEY, original)


@pytest.mark.parametrize("attribute", ["sqlstate", "pgcode"])
async def test_queue_timeout_rolls_back_savepoint_and_raises_lock_busy(attribute, timer):
    error = database_error(attribute, "55P03")
    session = Session(results=[False], error=error)
    with pytest.raises(locks.LockBusy, match="held by other writers") as caught:
        await locks.lock_data_type(session, DATA_TYPE, exclusive=True, wait="0ms")
    assert caught.value.__cause__ is error
    assert session.calls == [(TRY_XACT, DATA_KEY)] + queued_calls("rollback savepoint")
    assert timer.pauses == []


async def test_other_database_error_propagates_after_savepoint_rollback(timer):
    error = database_error("sqlstate", "40P01")
    session = Session(results=[False], error=error)
    with pytest.raises(DBAPIError) as caught:
        await locks.lock_data_type(session, DATA_TYPE, exclusive=True, wait="0ms")
    assert caught.value is error
    assert session.calls == [(TRY_XACT, DATA_KEY)] + queued_calls("rollback savepoint")


async def test_polling_database_error_propagates_without_queueing(timer):
    error = database_error("sqlstate", "08006")
    session = Session(results=[error])
    with pytest.raises(DBAPIError) as caught:
        await locks.lock_data_type(session, DATA_TYPE, exclusive=True, wait="100ms")
    assert caught.value is error
    assert session.calls == [(TRY_XACT, DATA_KEY)]


@pytest.mark.parametrize("mode", ["dry-run", "sqlite", "unbound"])
async def test_single_run_bypass_opens_no_connection(mode):
    connection = Connection()
    sessions = factory(connection, dialect="sqlite" if mode == "sqlite" else "postgresql")
    engine = sessions.kw["bind"]
    if mode == "unbound":
        sessions.kw.clear()
    async with locks.backfill_lock(sessions, dry_run=mode == "dry-run"):
        engine.connect.assert_not_awaited()
    engine.connect.assert_not_awaited()
    assert connection.calls == []
    connection.close.assert_not_awaited()


@pytest.mark.parametrize("body_fails", [False, True])
async def test_single_run_unlocks_and_closes_on_success_and_body_error(body_fails):
    connection = Connection()
    sessions = factory(connection)

    async def run():
        async with locks.backfill_lock(sessions):
            assert connection.calls == [
                ("options", {"isolation_level": "AUTOCOMMIT"}),
                (
                    "SELECT set_config('application_name', :name, false)",
                    {"name": "healthos-identity-backfill"},
                ),
                (TRY_RUN, RUN_KEY),
            ]
            connection.close.assert_not_awaited()
            if body_fails:
                raise ValueError("body failed")

    if body_fails:
        with pytest.raises(ValueError, match="body failed"):
            await run()
    else:
        await run()
    assert connection.calls[-1] == (UNLOCK, RUN_KEY)
    sessions.kw["bind"].connect.assert_awaited_once()
    connection.close.assert_awaited_once()


async def test_single_run_waits_in_half_second_steps_then_acquires(timer):
    connection = Connection(results=[False, False, True])
    async with locks.backfill_lock(factory(connection), purpose="identity-decide", wait=2):
        assert timer.pauses == [0.5, 0.5]
    assert connection.calls[1][1] == {"name": "healthos-identity-decide"}
    assert connection.calls[2:-1] == [(TRY_RUN, RUN_KEY)] * 3
    assert connection.calls[-1] == (UNLOCK, RUN_KEY)
    connection.close.assert_awaited_once()


async def test_single_run_timeout_names_holder_and_closes_without_unlocking(timer):
    connection = Connection(
        results=[False] * 3,
        holder=("healthos-identity-backfill", datetime(2026, 1, 1, tzinfo=UTC)),
    )
    with pytest.raises(locks.BackfillRunning, match="an identity-backfill run") as caught:
        async with locks.backfill_lock(factory(connection), purpose="identity-decide", wait=0.75):
            pytest.fail("busy lock must not enter body")
    assert timer.pauses == [0.5, 0.25]
    assert "(waited 0.75 s)" in str(caught.value)
    assert "connected since 2026-01-01 00:00:00 UTC" in str(caught.value)
    assert "Run the identity-decide again" in str(caught.value)
    assert "1212500274/1" in str(caught.value)
    assert (UNLOCK, RUN_KEY) not in connection.calls
    connection.close.assert_awaited_once()


@pytest.mark.parametrize(
    "name, expected",
    [
        (None, "a session (unnamed)"),
        ("custom", "a session (custom)"),
        ("healthos-identity-decide", "another identity-decide"),
    ],
)
async def test_single_run_refusal_describes_other_holders(name, expected, timer):
    connection = Connection(results=[False], holder=(name, datetime(2026, 1, 1, tzinfo=UTC)))
    with pytest.raises(locks.BackfillRunning) as caught:
        async with locks.backfill_lock(factory(connection)):
            pytest.fail("busy lock must not enter body")
    assert expected in str(caught.value)
    assert "waited" not in str(caught.value)
    assert timer.pauses == []
    connection.close.assert_awaited_once()


@pytest.mark.parametrize(
    "fail_at", ["options", "SELECT set_config('application_name', :name, false)", TRY_RUN, UNLOCK]
)
async def test_single_run_closes_connection_on_setup_acquisition_or_unlock_error(fail_at):
    connection = Connection(fail_at=fail_at)
    with pytest.raises(RuntimeError, match="connection failed"):
        async with locks.backfill_lock(factory(connection)):
            pass
    assert ((UNLOCK, RUN_KEY) in connection.calls) is (fail_at == UNLOCK)
    connection.close.assert_awaited_once()


async def test_single_run_disappearing_holder_still_refuses_and_closes():
    connection = Connection(results=[False])
    with pytest.raises(locks.BackfillRunning, match="another identity-backfill or identity-decide"):
        async with locks.backfill_lock(factory(connection)):
            pytest.fail("busy lock must not enter body")
    assert (UNLOCK, RUN_KEY) not in connection.calls
    connection.close.assert_awaited_once()
