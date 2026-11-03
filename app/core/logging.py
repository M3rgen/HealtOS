from __future__ import annotations

import logging
import re
import sys

from pythonjsonlogger.json import JsonFormatter

SENSITIVE_NAMES = {"access_token", "refresh_token", "client_secret", "authorization"}


class RedactSecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for name in SENSITIVE_NAMES:
            if hasattr(record, name):
                setattr(record, name, "[REDACTED]")
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s %(event)s"))
    handler.addFilter(RedactSecrets())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # httpx INFO messages include complete request URLs. Those URLs may contain
    # resumable pagination tokens, so keep transport logging to warnings/errors.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


MAX_CHAIN = 6
_SQLSTATE = re.compile(r"[0-9A-Z]{5}")


def error_diagnostics(exc: BaseException) -> dict[str, str]:
    """Value-free failure diagnostics for log records: class names and SQLSTATE only.

    Exception text, arguments and SQL parameters can carry raw health records, so only
    the cause/context chain's class names and the database's five-character code leave.
    """
    names: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(names) < MAX_CHAIN:
        names.append(type(current).__name__)
        # MAX_CHAIN also bounds a cyclic chain.
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    result = {"error_chain": " < ".join(names)}
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    if isinstance(sqlstate, str) and _SQLSTATE.fullmatch(sqlstate):
        result["sqlstate"] = sqlstate
    return result


def event(logger: logging.Logger, name: str, **fields: object) -> None:
    logger.info(name, extra={"event": name, **fields})
