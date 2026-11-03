"""Container signal delivery and cancellation through the application lifespan."""

import asyncio
import json
import os
import select as io_select
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from reconciliation_records import Client, point, service
from sqlalchemy import func, select

from app import main
from app.core.config import Settings
from app.google_health.client import Page
from app.models.state import JobStatus, RawHealthData, SyncState
from app.services import reconciliation_reports as reports
from app.services.runtime import build_runtime

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("migration_code", [0, 1])
def test_container_command_delivers_sigterm_without_a_shell_parent(tmp_path, migration_code):
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    command = json.loads(next(line[4:] for line in lines if line.startswith("CMD ")))
    for name, content in {
        "alembic": f"#!/bin/sh\nexit {migration_code}\n",
        "uvicorn": f"#!{sys.executable}\nimport os, signal\n"
        "signal.signal(signal.SIGTERM, lambda *_: exit(0))\n"
        "print(os.getpid(), flush=True)\nsignal.pause()\n",
    }.items():
        script = tmp_path / name
        script.write_text(content)
        script.chmod(0o755)
    environment = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]}
    process = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE)
    try:
        if migration_code:
            assert process.communicate(timeout=5)[0] == b""
            assert process.returncode == migration_code
        else:
            assert io_select.select([process.stdout], [], [], 5)[0], "server did not start"
            assert int(process.stdout.readline()) == process.pid
            process.terminate()
            assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert compose["services"]["app"]["stop_grace_period"] == "20s"
    assert "--timeout-graceful-shutdown 5" in command[-1]


@pytest.mark.parametrize("scheduled", [False, True])
async def test_lifespan_waits_for_reconciliation_cleanup_and_checkpoint_resumes(
    sessions, monkeypatch, scheduled
):
    waiting, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    record = point()

    class Waiting(Client):
        async def list_data_points(self, spec, start, end, page_token=None):
            if page_token:
                waiting.set()
                await asyncio.Event().wait()
            return Page([record], "second")

    settings = Settings(_env_file=None, app_env="test", watchdog_enabled=False)
    runtime = build_runtime(settings, sessions)
    runtime.reconciliation = service(sessions, Waiting())
    monkeypatch.setattr(runtime, "scheduler_needed", lambda _: scheduled)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    monkeypatch.setattr(main, "build_runtime", lambda *_: runtime)
    finish = reports.finish_run

    async def delayed_finish(*args):
        cleaning.set()
        await release.wait()
        return await finish(*args)

    monkeypatch.setattr(reports, "finish_run", delayed_finish)
    app = main.create_app()
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    if scheduled:
        runtime.scheduler.add_job(runtime.reconciliation.run, kwargs={"days": 1})
    else:
        runtime.spawn(runtime.reconciliation.run(1))
    shutdown = None
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
        await asyncio.wait_for(cleaning.wait(), 5)
        assert not shutdown.done()
        assert not runtime.google.http.is_closed
        release.set()
        await asyncio.wait_for(shutdown, 5)
        assert runtime.google.http.is_closed and runtime.oauth.http.is_closed
        assert runtime.webhook_verifier.http.is_closed and not runtime.tasks
        async with sessions() as session:
            run = (await reports.list_runs(session))["runs"][0]
            state = await session.get(SyncState, "heart-rate")
        assert run["status"] == "cancelled" and run["types"][0]["pages_committed"] == 1
        assert state.last_page_token == "second"
        # A fresh service resumes the committed window. Repeated source records remain unique.
        restarted = service(sessions, Client(Page([record, point()], None)))
        await restarted.incremental.run(
            "heart-rate", state.last_requested_from, state.last_requested_to
        )
        assert restarted.incremental.client.tokens == ["second"]
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 2
            state = await session.get(SyncState, "heart-rate")
        assert state.status == JobStatus.completed and state.last_page_token is None
    finally:
        release.set()
        if shutdown:
            await shutdown
        else:
            await lifespan.__aexit__(None, None, None)
