"""Real synthetic subprocesses exercise the service entry point, without a CLI/account."""

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import start_codex_bridge as bridge

FAKE_CLI = """\
import json, os, signal, subprocess, sys, time
from pathlib import Path
home = Path(os.environ["CODEX_HOME"])
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
stopping = False
def stop(*args):
    global stopping
    stopping = True
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
(home / "started").write_text(json.dumps({
    "pid": os.getpid(), "child": child.pid, "args": sys.argv[1:],
    "has_api_key": "OPENAI_API_KEY" in os.environ,
    "cwd": str(Path.cwd()),
}))
while not stopping:
    if (home / "exit-code").exists():
        sys.exit(int((home / "exit-code").read_text()))
    time.sleep(0.02)
if os.environ.get("FAKE_IGNORE_STOP"):
    while True:
        time.sleep(1)
child.terminate()
child.wait()
(home / "stopped").write_text("clean")
"""


def wait_for(predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.03)
    raise AssertionError("synthetic service did not reach the expected state")


def running(pid):
    try:
        # A terminated descendant may await reaping by the host's init, but cannot run.
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
    except FileNotFoundError:
        return False


@pytest.fixture
def service(tmp_path):
    from contextlib import contextmanager

    script = tmp_path / "scripts" / "start_codex_bridge.py"
    script.parent.mkdir()
    script.write_text(Path(bridge.__file__).read_text())
    cli = tmp_path / "codex"
    cli.write_text(f"#!{sys.executable}\n" + FAKE_CLI)
    cli.chmod(0o700)
    source = tmp_path / "identity"
    source.mkdir()
    (source / "auth.json").write_text("{}")
    state = source / "healthos-bridge"
    state.mkdir()
    token = secrets.token_urlsafe(32)
    (state / "bridge.token").write_text(token)
    env = dict(
        os.environ,
        CODEX_HOME=str(source),
        CODEX_BRIDGE_BIND_HOST="127.0.0.1",
        PATH=str(tmp_path) + os.pathsep + os.environ["PATH"],
        OPENAI_API_KEY="synthetic",
    )
    env.pop("CODEX_BRIDGE_TOKEN", None)

    @contextmanager
    def start(expected=0, ready=True, **overrides):
        marker = state / "started"
        marker.unlink(missing_ok=True)
        process = subprocess.Popen(
            [sys.executable, str(script), "--foreground"],
            env=dict(env, **overrides),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        data = None
        try:
            if ready:
                wait_for(lambda: marker.exists() and marker.stat().st_size)
                data = json.loads(marker.read_text())
            yield process, data
        finally:
            if process.poll() is None:
                process.terminate()
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == expected, stderr.decode()
            assert token.encode() not in stdout + stderr
            if data:
                wait_for(lambda: not running(data["pid"]) and not running(data["child"]))

    return start, state, token


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
def test_service_start_stop_restart_preserves_identity(service, stop_signal):
    start, state, token = service
    for _ in range(2):
        with start() as (process, data):
            assert process.poll() is None and data["pid"] != process.pid
            assert data["cwd"] == str(state / "workspace") and not data["has_api_key"]
            assert data["args"][-2:] == ["--ws-token-file", str(state / "bridge.token")]
            assert data["args"][:3] == ["app-server", "--listen", "ws://127.0.0.1:8766"]
            assert (state / "bridge.token").read_text() == token
            for name in ("bridge.token", "auth.json", "config.toml"):
                assert (state / name).stat().st_mode & 0o777 == 0o600
            assert state.stat().st_mode & 0o777 == 0o700
            process.send_signal(stop_signal)
        assert (state / "stopped").read_text() == "clean"
        assert not (state.parents[1] / ".env").exists()


@pytest.mark.parametrize("exit_code", [0, 7])
def test_unexpected_exit_fails_cleans_descendants_and_can_restart(service, exit_code):
    start, state, _ = service
    with start(expected=exit_code or 1) as (process, data):
        (state / "exit-code").write_text(str(exit_code))
        process.wait(timeout=10)
    (state / "exit-code").unlink()
    with start() as (_, replacement):
        assert replacement["pid"] != data["pid"]


def test_service_escalates_a_stop_and_requires_an_existing_token(service):
    start, state, _ = service
    with start(FAKE_IGNORE_STOP="1"):
        pass  # fixture cleanup sends SIGTERM and verifies the complete process group stops
    (state / "bridge.token").unlink()
    with start(expected=1, ready=False) as (process, _):
        process.wait(timeout=10)
        assert not (state / "started").exists()


def test_service_starts_when_the_operators_env_file_is_unreadable(service):
    """systemd runs as healthos-bridge; the checkout's chmod 600 .env belongs to the operator."""
    start, state, _ = service
    path = state.parents[1] / ".env"
    path.write_text("CODEX_BRIDGE_URL=ws://operator.invalid:8766\n")
    path.chmod(0)
    try:
        # No bind host in the environment: address selection consults .env first.
        with start(CODEX_BRIDGE_BIND_HOST="") as (process, data):
            assert data["args"][:2] == ["app-server", "--listen"]
            process.send_signal(signal.SIGTERM)
        assert (state / "stopped").read_text() == "clean"
    finally:
        path.chmod(0o600)


def test_unreadable_env_file_reads_as_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "ROOT", tmp_path)
    monkeypatch.delenv("CODEX_BRIDGE_BIND_HOST", raising=False)
    monkeypatch.setattr(bridge, "docker0_address", lambda: None)
    path = tmp_path / ".env"
    path.write_text("CODEX_BRIDGE_BIND_HOST=192.0.2.1\n")
    original = Path.read_text

    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError(13, "Permission denied", str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    assert bridge.env_file_value("CODEX_BRIDGE_BIND_HOST") == ""
    assert bridge.bind_host() == "127.0.0.1"


@pytest.mark.parametrize("configured", [False, True])
def test_manual_launcher_reuses_a_healthy_bridge_and_preserves_url(
    tmp_path, monkeypatch, configured
):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(bridge, "ROOT", tmp_path)
    monkeypatch.delenv("CODEX_BRIDGE_URL", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "identity"))
    monkeypatch.setenv("CODEX_BRIDGE_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN", "synthetic")
    monkeypatch.setattr(sys, "argv", ["start_codex_bridge.py"])
    monkeypatch.setattr(bridge, "healthy", AsyncMock(return_value=True))
    path = tmp_path / ".env"
    path.write_text("OTHER_SETTING=keep\n  CODEX_BRIDGE_URL=ws://custom.invalid:9999\n")
    expected = "ws://environment.invalid:9998" if configured else "ws://custom.invalid:9999"
    if configured:
        monkeypatch.setenv("CODEX_BRIDGE_URL", expected)
    previous_umask = os.umask(0o077)
    try:
        for _ in range(2):
            bridge.main()  # no source login exists: only healthy reuse can succeed
    finally:
        os.umask(previous_umask)
    assert path.read_text().splitlines() == [
        "OTHER_SETTING=keep",
        "CODEX_BRIDGE_TOKEN=synthetic",
        f"CODEX_BRIDGE_URL={expected}",
    ]
    assert path.stat().st_mode & 0o777 == 0o600


def test_service_restart_keeps_refreshed_login_token_and_custom_url(service):
    start, state, _ = service
    path = state.parents[1] / ".env"
    configuration = "CODEX_BRIDGE_URL=ws://custom.invalid:9988\nOTHER_SETTING=keep\n"
    path.write_text(configuration)
    configured = secrets.token_urlsafe(32)
    (state / "bridge.token").unlink()
    with start(CODEX_BRIDGE_TOKEN=configured):
        assert (state / "bridge.token").read_text() == configured
    refreshed = '{"synthetic_refresh_generation": 2}'
    (state / "auth.json").write_text(refreshed)
    (state.parent / "auth.json").unlink()
    with start():
        assert (state / "auth.json").read_text() == refreshed
        assert (state / "bridge.token").read_text() == configured
        assert path.read_text() == configuration


def test_private_write_failure_preserves_previous_identity(tmp_path, monkeypatch):
    path = tmp_path / "bridge.token"
    path.write_text("synthetic-original")

    def fail(*args):
        raise OSError("synthetic interrupted replacement")

    monkeypatch.setattr(bridge.os, "replace", fail)
    with pytest.raises(OSError):
        bridge.write_private(path, "synthetic-replacement")
    assert path.read_text() == "synthetic-original"
    assert list(tmp_path.iterdir()) == [path]
