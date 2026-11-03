"""/health flags rejected Google records until the maintainer acknowledges them."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from cli_records import cli_database as cli_database
from fastapi import FastAPI
from raw_response_records import SPEC, steps
from rejection_records import bad_record
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from typer.testing import CliRunner

from app.api.health import router
from app.cli import main as cli
from app.core.config import Settings
from app.models.raw_rejections import RawRejectionAck
from app.models.state import JobStatus, SyncState
from app.services import raw_rejections
from app.services.pipeline_health import PipelineHealthService, PipelineStatus
from app.services.storage import store_records

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
RUNNER = CliRunner()


async def fresh(sessions, now=NOW):
    async with sessions.begin() as session:
        session.add(
            SyncState(
                data_type="steps",
                status=JobStatus.completed,
                last_successful_sync=now - timedelta(minutes=5),
                records_downloaded=0,
            )
        )


async def inspect(sessions):
    settings = Settings(_env_file=None, pipeline_stale_threshold_minutes=120)
    return await PipelineHealthService(settings, sessions, {"steps"}).inspect(now=NOW)


async def reject(sessions, *records):
    async with sessions.begin() as session:
        await store_records(session, SPEC, list(records))


async def latest(sessions) -> int:
    async with sessions() as session:
        return (await raw_rejections.summary(session))["latest_id"]


async def ack(sessions, through):
    async with sessions.begin() as session:
        return await raw_rejections.acknowledge(session, through)


async def test_acknowledgement_turns_health_green_until_a_new_record_is_rejected(sessions):
    await fresh(sessions)
    assert (await inspect(sessions)).status == PipelineStatus.healthy

    await reject(sessions, bad_record("nul"), steps(1))
    snapshot = await inspect(sessions)
    assert snapshot.status == PipelineStatus.degraded
    assert snapshot.reasons == ("rejected_google_records",)
    assert snapshot.as_dict()["rejected_records"] == 1

    assert await ack(sessions, await latest(sessions)) == (1, True)
    snapshot = await inspect(sessions)
    assert snapshot.status == PipelineStatus.healthy and snapshot.rejected_records == 0

    # The same record sent again (another place, so another row) is already acknowledged.
    await reject(sessions, steps(1), bad_record("nul"))
    assert await latest(sessions) == 2
    assert (await inspect(sessions)).status == PipelineStatus.healthy

    await reject(sessions, bad_record("overflow"))
    snapshot = await inspect(sessions)
    assert snapshot.status == PipelineStatus.degraded and snapshot.rejected_records == 1
    # A rejection never hides a worse state.
    async with sessions.begin() as session:
        (await session.get(SyncState, "steps")).status = JobStatus.failed
    snapshot = await inspect(sessions)
    assert snapshot.status == PipelineStatus.failed
    assert snapshot.reasons == ("failed_sync_states", "rejected_google_records")


async def test_acknowledgements_only_move_forward_and_are_append_only(sessions):
    await reject(sessions, bad_record("nul"), bad_record("surrogate"))
    with pytest.raises(raw_rejections.UnknownRejection):
        await ack(sessions, 99)
    assert await ack(sessions, 2) == (2, True)
    assert await ack(sessions, 1) == (2, False)
    assert await ack(sessions, 2) == (2, False)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RawRejectionAck)) == 1
    for statement in (
        "UPDATE raw_rejection_acks SET through_id = 1",
        "DELETE FROM raw_rejection_acks",
    ):
        with pytest.raises(DBAPIError, match="refused"):
            async with sessions.begin() as session:
                await session.execute(text(statement))


async def test_health_endpoint_reports_the_unacknowledged_records(sessions):
    await fresh(sessions, datetime.now(UTC))
    await reject(sessions, bad_record("nul_key"))
    settings = Settings(_env_file=None, app_env="test")
    app = FastAPI()
    app.include_router(router)
    app.state.settings = settings
    app.state.runtime = SimpleNamespace(
        scheduler_needed=lambda _: False,
        scheduler_running=False,
        pipeline_health=PipelineHealthService(settings, sessions, {"steps"}),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as http:
        payload = (await http.get("/health")).json()
    assert payload["status"] == "degraded" and payload["rejected_records"] == 1
    assert "rejected_google_records" in payload["reasons"]


def test_cli_lists_exports_and_acknowledges_in_turkish(cli_database):
    asyncio.run(reject(cli_database, bad_record("surrogate"), steps(1)))

    listed = RUNNER.invoke(cli.app, ["rejections"])
    assert listed.exit_code == 0, listed.output
    report = json.loads(listed.stdout)
    assert (report["unacknowledged"], report["acknowledged_through"]) == (1, 0)
    (row,) = report["rejections"]
    assert row["id"] == 1 and row["data_type"] == "steps" and row["acknowledged"] is False
    # Metadata only: the record's values are not listed.
    assert "private-marker" not in listed.stdout

    exported = RUNNER.invoke(cli.app, ["rejections", "--element", "1"])
    assert exported.exit_code == 0
    assert json.loads(exported.stdout_bytes) == bad_record("surrogate")

    acknowledged = RUNNER.invoke(cli.app, ["rejections", "--ack", "1"])
    assert acknowledged.exit_code == 0
    assert acknowledged.stdout.startswith("Onaylandı: 1 numaralı reddedilen kayda kadar")
    again = RUNNER.invoke(cli.app, ["rejections", "--ack", "1"])
    assert again.stdout.startswith("Zaten onaylı: onay 1 numaralı kayda kadar")
    assert json.loads(RUNNER.invoke(cli.app, ["rejections"]).stdout)["unacknowledged"] == 0

    for arguments, code, message in (
        (["--ack", "5"], 1, "Reddedilen kayıt bulunamadı: 5\n"),
        (["--element", "5"], 1, "Reddedilen kayıt bulunamadı: 5\n"),
        (["--ack", "1", "--element", "1"], 2, "--ack ve --element birlikte kullanılamaz.\n"),
    ):
        result = RUNNER.invoke(cli.app, ["rejections", *arguments])
        assert (result.exit_code, result.stderr, result.stdout) == (code, message, "")
    for result in (listed, exported, acknowledged, again):
        assert "Codex" not in result.output
