"""Import compatibility for the extracted identity schema verification."""

import importlib
import subprocess
import sys

import pytest


def test_schema_import_does_not_load_identity_or_backfill() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.services import identity_schema; "
            "import sys; "
            "assert 'app.services.identity' not in sys.modules; "
            "assert 'app.services.identity_backfill' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "name",
    [
        "identity_tables_missing",
        "verify_identity_schema",
        "verify_views",
        "EXPECTED_CONSTRAINTS",
        "_VIEWS_SQL",
    ],
)
def test_legacy_exports_are_the_extracted_implementations(name: str) -> None:
    schema = importlib.import_module("app.services.identity_schema")
    legacy = importlib.import_module("app.services.identity")
    exported = getattr(schema, name)

    assert getattr(legacy, name) is exported
    if callable(exported):
        assert exported.__module__ == schema.__name__
