"""Public archive entry points keep working across the raw-history module split."""

import asyncio
import importlib
import json
import subprocess
import sys

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_raw_versions import NAME, SPEC, _legacy_dump, chain, heart_rate, store
from typer.testing import CliRunner

from app.cli import main as cli
from app.db.base import Base
from app.models.raw_versions import RawHealthDataVersion
from app.models.state import RawHealthData
from app.parsers.common import payload_hash
from app.services import raw_history, raw_history_writing


@pytest.mark.parametrize(
    ("module", "names"),
    [
        ("import", "import_versions"),
        ("source", "source_database_url newest_sighting"),
        ("types", "ImportStats IMPORT_SOURCE"),
        (
            "verification",
            "verify_versions verify_guards PG_TRIGGER_SQL PROTECTIVE_TRIGGERS "
            "SQLITE_TRIGGER_SQL FUNCTION_BODIES",
        ),
    ],
)
def test_existing_exports_use_the_extracted_implementations(module, names):
    implementation = importlib.import_module(f"app.services.raw_history_{module}")
    for name in names.split():
        assert getattr(raw_history, name) is getattr(implementation, name)


@pytest.mark.parametrize(
    "first", ["import", "source", "timeline", "types", "verification", "writing"]
)
def test_modules_import_before_the_facade_and_production_callers(first):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import app.services.raw_history_{first}; import sys; "
            "assert 'app.services.raw_history' not in sys.modules; "
            "import app.services.raw_history; import app.services.identity_verification; "
            "import app.cli.main",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_import_preserves_recurrences_and_verifies(tmp_path, monkeypatch, dry_run):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'target.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def prepare():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await store(sessions, heart_rate(90))

    asyncio.run(prepare())
    monkeypatch.setattr(cli, "SessionLocal", sessions)
    arguments = ["import-versions", "--batch-size", "1"]
    # Multiple dumps contain the same payload on each side of a different payload.
    for day, bpm in ((3, 60), (2, 70), (1, 60)):
        record = heart_rate(bpm)
        url = _legacy_dump(
            tmp_path,
            monkeypatch,
            f"day-{day}",
            [(NAME, record, payload_hash(record), f"2026-08-0{day} 00:00:00")],
        )
        arguments.extend(["--from-db", url])
    if dry_run:
        arguments.append("--dry-run")
    runner = CliRunner()
    try:
        result = runner.invoke(cli.app, arguments)
        assert result.exit_code == 0, result.output
        report = json.loads(result.output)
        assert report["dry_run"] is dry_run
        assert [item["database"].rsplit("/", 1)[-1] for item in report["sources"]] == [
            "day-3.db",
            "day-2.db",
            "day-1.db",
        ]
        assert [item["imported"] for item in report["sources"]] == [1, 1, 1]
        assert all(isinstance(item["source_rows"], int) for item in report["sources"])
        versions = asyncio.run(chain(sessions))
        assert [item[2] for item in versions] == ([90] if dry_run else [60, 70, 60, 90])
        assert [item[0] for item in versions] == list(range(1, len(versions) + 1))
        verified = runner.invoke(cli.app, ["verify"])
        assert verified.exit_code == 0, verified.output
        assert f"{0 if dry_run else 3} earlier versions" in verified.output
        if not dry_run:
            again = runner.invoke(cli.app, arguments)
            assert again.exit_code == 0, again.output
            assert all(item["imported"] == 0 for item in json.loads(again.output)["sources"])
        history = runner.invoke(cli.app, ["history", "--data-type", SPEC.name, "--id", NAME])
        assert history.exit_code == 0, history.output
        assert len(json.loads(history.output)[0]["versions"]) == len(versions)
    finally:
        asyncio.run(engine.dispose())


async def test_write_verification_failure_rolls_back_inserts_and_renumbering(
    sessions, tmp_path, monkeypatch
):
    await store(sessions, heart_rate(70))
    await store(sessions, heart_rate(90))
    record = heart_rate(60)
    source = create_async_engine(
        _legacy_dump(
            tmp_path,
            monkeypatch,
            "older",
            [(NAME, record, payload_hash(record), "2026-08-01 00:00:00")],
        )
    )

    async def archive_rows():
        async with sessions() as session:
            return [
                (await session.execute(select(table).order_by(table.c.id))).all()
                for table in (RawHealthData.__table__, RawHealthDataVersion.__table__)
            ]

    before = await archive_rows()
    # Fail the read-back check after the writer parked an old number and inserted
    # the imported payload. The coordinator must roll back the whole chunk.
    monkeypatch.setattr(raw_history_writing, "payload_hash", lambda value: "changed-on-read")
    try:
        with pytest.raises(RuntimeError, match="imported payload would not read back unchanged"):
            await raw_history.import_versions(sessions, source, batch_size=1)
        assert await archive_rows() == before
        async with sessions() as session:
            assert await raw_history.verify_versions(session) == (1, [])
    finally:
        await source.dispose()
