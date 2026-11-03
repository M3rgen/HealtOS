"""Every test starts from one root-logger baseline, whatever ran or was imported first."""

import logging

from app.core.logging import RedactSecrets, configure_logging

# tests/conftest.py ROOT_TEST_LEVEL; "conftest" is ambiguous as an import name.
ROOT_TEST_LEVEL = logging.INFO


def app_handlers():
    return [
        handler
        for handler in logging.getLogger().handlers
        if any(isinstance(item, RedactSecrets) for item in handler.filters)
    ]


def test_configure_logging_changes_root_inside_a_test():
    configure_logging("ERROR")
    assert logging.getLogger().level == logging.ERROR and len(app_handlers()) == 1
    logging.getLogger("httpx").setLevel(logging.DEBUG)


def test_next_test_starts_from_the_baseline_again():
    assert logging.getLogger().level == ROOT_TEST_LEVEL and app_handlers() == []
    assert logging.getLogger("httpx").level == logging.WARNING


def test_caplog_sees_info_without_set_level(caplog):
    logging.getLogger("app.synthetic").info("synthetic-info-event")
    assert "synthetic-info-event" in caplog.text
