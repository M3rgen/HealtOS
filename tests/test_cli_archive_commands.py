"""Archive CLI streams, schema gates and source-engine cleanup keep their contracts."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from cli_records import cli_database as cli_database
from typer.testing import CliRunner

from app.cli import main as cli
from app.models.raw_versions import RawApiResponse
from app.services import identity, manual_flags, raw_history

RUNNER = CliRunner()


@pytest.mark.parametrize("destination", ["stdout", "file"])
@pytest.mark.parametrize("valid", [False, True])
def test_export_preserves_binary_bytes_and_stderr_metadata(
    cli_database, tmp_path, destination, valid
):
    body = b"\x00\xff\x80\r\n" + "ölçüm".encode()

    async def seed():
        async with cli_database.begin() as session:
            session.add(
                RawApiResponse(
                    fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
                    source="probe",
                    context_id="test",
                    data_type="steps",
                    method="GET",
                    path="/test",
                    attempt=0,
                    status_code=200,
                    headers={"content-encoding": "gzip"},
                    body=body,
                    body_sha256=hashlib.sha256(body).hexdigest() if valid else "0" * 64,
                    body_size=len(body),
                    complete=True,
                )
            )

    asyncio.run(seed())
    target = tmp_path / "response.bin"
    args = ["export-response", "1"]
    if destination == "file":
        target.write_bytes(b"existing output")
        args.extend(["-o", str(target)])
    result = RUNNER.invoke(cli.app, args)
    assert result.exit_code == (0 if valid else 1)
    assert result.stdout_bytes == (body if valid and destination == "stdout" else b"")
    metadata, *errors = result.stderr.splitlines()
    assert json.loads(metadata)["body_size"] == len(body)
    assert json.loads(metadata)["content_encoding"] == "gzip"
    assert errors == ([] if valid else ["the stored body no longer matches its SHA-256"])
    if destination == "file":
        assert target.read_bytes() == (body if valid else b"existing output")
    # Metadata filters use UTC and preserve JSON without leaking the binary body.
    listed = RUNNER.invoke(
        cli.app,
        [
            "responses",
            "--data-type",
            "steps",
            "--context",
            "test",
            "--since",
            "2026-01-01",
        ],
    )
    assert listed.exit_code == 0 and listed.stderr == ""
    assert json.loads(listed.stdout) == [json.loads(metadata)]


def test_missing_records_keep_empty_stdout_and_error_codes(cli_database):
    exported = RUNNER.invoke(cli.app, ["export-response", "42"])
    assert exported.exit_code == 1
    assert exported.stdout_bytes == b""
    assert exported.stderr == "no stored response 42\n"
    history = RUNNER.invoke(cli.app, ["history", "--data-type", "steps", "--id", "missing"])
    assert history.exit_code == 1
    assert history.stdout == "[]\n"
    assert history.stderr == "no record steps/missing\n"


def test_history_forwards_read_only_filters_and_keeps_unicode(cli_database, monkeypatch):
    report = [{"label": "ölçüm"}]
    read = AsyncMock(return_value=report)
    monkeypatch.setattr(raw_history, "record_history", read)
    result = RUNNER.invoke(
        cli.app,
        [
            "history",
            "--data-type",
            "steps",
            "--id",
            "sample",
            "--since",
            "2026-01-02T03:04:05",
            "--limit",
            "1",
            "--no-payload",
        ],
    )
    assert result.exit_code == 0 and result.stderr == ""
    assert result.stdout == json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    assert read.await_args.args[1] == "steps"
    assert read.await_args.kwargs == {
        "google_id": "sample",
        "since": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        "limit": 1,
        "include_payload": False,
    }


@pytest.mark.parametrize("missing_identity", [False, True])
def test_verify_schema_gates_stop_before_guard_and_record_queries(
    cli_database, monkeypatch, missing_identity
):
    monkeypatch.setattr(
        identity, "identity_tables_missing", AsyncMock(return_value=missing_identity)
    )
    identities = AsyncMock(return_value=(0, ["identity schema needs upgrade"], ["older archive"]))
    flags = AsyncMock(return_value=["flag schema needs upgrade"])
    guards = AsyncMock(side_effect=AssertionError("must stop before guards"))
    monkeypatch.setattr(identity, "verify_identities", identities)
    monkeypatch.setattr(manual_flags, "flag_schema_errors", flags)
    monkeypatch.setattr(raw_history, "verify_guards", guards)
    result = RUNNER.invoke(cli.app, ["verify"])
    assert result.exit_code == 1
    assert result.stdout.startswith("Verified 0 raw records, 0 earlier versions, ")
    assert result.stderr == (
        "note: older archive\nidentity schema needs upgrade\n"
        if missing_identity
        else "flag schema needs upgrade\n"
    )
    assert flags.await_count == int(not missing_identity)
    assert identities.await_count == int(missing_identity)
    guards.assert_not_awaited()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("failure", ["report", "exception", "sighting"])
def test_import_failure_reports_and_disposes_every_source(
    cli_database, monkeypatch, dry_run, failure
):
    import sqlalchemy.ext.asyncio as db

    sources = [SimpleNamespace(dispose=AsyncMock()), SimpleNamespace(dispose=AsyncMock())]
    create = Mock(side_effect=sources)
    monkeypatch.setattr(db, "create_async_engine", create)
    monkeypatch.setattr(raw_history, "source_database_url", lambda value, _: f"sqlite:///{value}")
    newest = AsyncMock(side_effect=[None, datetime(2026, 1, 1, tzinfo=UTC)])
    operation = AsyncMock(
        return_value=raw_history.ImportStats(source_hash_mismatches=["steps/test"])
    )
    failure_error = ValueError("synthetic source failure")
    if failure == "exception":
        operation.side_effect = failure_error
    elif failure == "sighting":
        newest.side_effect = failure_error
    monkeypatch.setattr(raw_history, "newest_sighting", newest)
    monkeypatch.setattr(raw_history, "import_versions", operation)
    args = ["import-versions", "--from-db", "older", "--from-db", "newer", "--batch-size", "1"]
    if dry_run:
        args.append("--dry-run")
    result = RUNNER.invoke(cli.app, args)
    assert result.exit_code == 1
    for source in sources:
        source.dispose.assert_awaited_once_with()
    if failure == "report":
        report = json.loads(result.stdout)
        assert report["dry_run"] is dry_run
        assert [item["database"] for item in report["sources"]] == ["newer", "older"]
        assert result.stderr == (
            "some dump rows were not imported (listed above); keep those dump files\n"
        )
    else:
        assert result.exception is failure_error
        assert result.stdout == result.stderr == ""
    if failure != "sighting":
        operation.assert_any_await(cli_database, sources[1], batch_size=1, dry_run=dry_run)
    else:
        operation.assert_not_awaited()
