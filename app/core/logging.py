from __future__ import annotations

import logging
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


def event(logger: logging.Logger, name: str, **fields: object) -> None:
    logger.info(name, extra={"event": name, **fields})
