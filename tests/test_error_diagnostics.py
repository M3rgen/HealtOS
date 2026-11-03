"""Failure diagnostics name exception classes and SQLSTATE codes, never values."""

from sqlalchemy.exc import IntegrityError

from app.core.logging import MAX_CHAIN, error_diagnostics

MARKER = "synthetic-private-value"


class DriverError(Exception):
    def __init__(self, sqlstate):
        super().__init__(MARKER)
        self.sqlstate = sqlstate


def wrapped(sqlstate):
    try:
        try:
            raise KeyError(MARKER)
        except KeyError as context:
            raise DriverError(sqlstate) from context
    except DriverError as driver:
        return IntegrityError("INSERT ... VALUES (:raw)", {"raw": MARKER}, driver)


def test_database_error_keeps_class_chain_and_sqlstate_only():
    error = wrapped("23505")
    error.__cause__ = error.orig
    assert error_diagnostics(error) == {
        "error_chain": "IntegrityError < DriverError < KeyError",
        "sqlstate": "23505",
    }


def test_untrusted_sqlstate_and_long_or_cyclic_chains_stay_bounded():
    for sqlstate in (MARKER, "2350", None, 23505):
        assert "sqlstate" not in error_diagnostics(wrapped(sqlstate))
    first, second = RuntimeError(MARKER), ValueError(MARKER)
    first.__cause__, second.__cause__ = second, first
    chain = error_diagnostics(first)["error_chain"].split(" < ")
    assert len(chain) == MAX_CHAIN and MARKER not in str(chain)


def test_suppressed_context_is_not_reported():
    try:
        try:
            raise KeyError(MARKER)
        except KeyError:
            raise RuntimeError(MARKER) from None
    except RuntimeError as error:
        assert error_diagnostics(error) == {"error_chain": "RuntimeError"}
