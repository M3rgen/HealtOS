"""Import compatibility for the extracted evidence and reporting helpers."""

import importlib
import subprocess
import sys

import pytest


def test_reporting_import_does_not_load_the_backfill_executor() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.services import identity_backfill_reporting as reporting; "
            "from app.services.identity_backfill_stats import BackfillStats; "
            "import sys; "
            "assert 'app.services.identity_backfill' not in sys.modules; "
            "assert reporting.BackfillStats is BackfillStats",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", ["_Timeline", "_together", "_full", "_report"])
def test_legacy_exports_are_the_extracted_implementations(name: str) -> None:
    reporting = importlib.import_module("app.services.identity_backfill_reporting")
    legacy = importlib.import_module("app.services.identity_backfill")
    helper = getattr(reporting, name)

    assert getattr(legacy, name) is helper
    assert helper.__module__ == reporting.__name__
