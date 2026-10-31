"""Start an authenticated Codex app-server for the local site.

Uses the installed Codex CLI and ChatGPT login. No API key is required.
Manual mode detaches and reuses a healthy bridge; --foreground supervises it.

The bridge never listens on all interfaces. CODEX_BRIDGE_BIND_HOST picks the
address; the default is the docker0 gateway on Linux Docker Engine (the address
`host.docker.internal:host-gateway` resolves to inside the app container) and
127.0.0.1 elsewhere (Docker Desktop forwards host.docker.internal to loopback).
Firewall example for a Linux host: deploy/firewall/.
"""

import argparse
import asyncio
import ipaddress
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8766


def docker0_address():
    """IPv4 of the default Docker bridge on Linux, or None."""
    if not sys.platform.startswith("linux") or not shutil.which("ip"):
        return None
    result = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "dev", "docker0"],
        capture_output=True,
        text=True,
        check=False,
    )
    for field in result.stdout.split():
        if "/" in field:
            try:
                return str(ipaddress.IPv4Interface(field).ip)
            except ValueError:
                continue
    return None


def env_file_value(name):
    path = ROOT / ".env"
    if not path.exists():
        return ""
    try:
        text = path.read_text("utf-8")
    except OSError:
        # The service user cannot read the operator's chmod 600 .env; its settings
        # come from EnvironmentFile=/etc/healthos/bridge.env instead.
        return ""
    values = [
        value.strip().strip("\"'")
        for key, _, value in (line.partition("=") for line in text.splitlines())
        if key.strip() == name
    ]
    return values[-1] if values else ""


def bind_host():
    host = (
        os.environ.get("CODEX_BRIDGE_BIND_HOST", "").strip()
        or env_file_value("CODEX_BRIDGE_BIND_HOST")
        or docker0_address()
        or "127.0.0.1"
    )
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError as exc:
        raise SystemExit(f"CODEX_BRIDGE_BIND_HOST bir IP adresi olmalı: {host!r}") from exc
    if address.is_unspecified:
        raise SystemExit(
            "Köprü tüm arayüzlerde dinleyemez; CODEX_BRIDGE_BIND_HOST için 127.0.0.1 "
            "ya da docker0 adresini kullan."
        )
    return f"[{address}]" if address.version == 6 else str(address)


def write_private(path, text):
    """Replace a private file atomically; interrupted writes keep the previous identity."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def update_env(token):
    path = ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replacements = {
        "CODEX_BRIDGE_TOKEN": token,
        "CODEX_BRIDGE_URL": os.environ.get("CODEX_BRIDGE_URL", "").strip()
        or env_file_value("CODEX_BRIDGE_URL")
        or f"ws://host.docker.internal:{PORT}",
    }
    lines = [line for line in lines if line.partition("=")[0].strip() not in replacements]
    write_private(
        path,
        "\n".join(lines + [f"{key}={value}" for key, value in replacements.items()]) + "\n",
    )


def supervise(command, workspace, child_env):
    """Own the child until stopped; even a spontaneous exit 0 is a service failure."""
    stopping = False
    process = None

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    def signal_group(signum):
        if process is not None:
            try:
                os.killpg(process.pid, signum)
            except (ProcessLookupError, PermissionError):
                # macOS answers EPERM for a group whose members are all zombies.
                pass

    handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        process = subprocess.Popen(
            command, cwd=workspace, env=child_env, stdin=subprocess.DEVNULL, start_new_session=True
        )
        while process.poll() is None and not stopping:
            time.sleep(0.1)
        if stopping:
            return 0
        print(
            f"Umay köprüsü beklenmedik biçimde durdu (çıkış {process.returncode}).",
            file=sys.stderr,
        )
        return process.returncode if process.returncode > 0 else 1
    finally:
        if process is not None:
            signal_group(signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            finally:
                signal_group(signal.SIGKILL)
                process.wait()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


async def healthy(token, host):
    from websockets.asyncio.client import connect

    try:
        async with connect(
            f"ws://{host}:{PORT}",
            open_timeout=2,
            additional_headers={"Authorization": f"Bearer {token}"},
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "clientInfo": {"name": "healtos_probe", "version": "0.1.0"},
                        },
                    }
                )
            )
            while True:
                reply = json.loads(await asyncio.wait_for(ws.recv(), 3))
                if reply.get("id") == 1:
                    return "result" in reply
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--foreground", action="store_true", help="Run under a Linux service manager"
    )
    foreground = parser.parse_args().foreground
    if foreground and os.name != "posix":
        raise SystemExit("Ön planda servis çalıştırma yalnız POSIX sistemlerde desteklenir.")
    os.umask(0o077)
    host = bind_host()
    source_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    bridge_home = source_home / "healthos-bridge"
    bridge_home.mkdir(parents=True, exist_ok=True)
    bridge_home.chmod(0o700)
    token_file = bridge_home / "bridge.token"
    token = (
        os.environ.get("CODEX_BRIDGE_TOKEN", "").strip()
        or (token_file.read_text().strip() if token_file.exists() else "")
        or env_file_value("CODEX_BRIDGE_TOKEN")
    )
    if not token and foreground:
        raise SystemExit("Servis için CODEX_BRIDGE_TOKEN veya mevcut bridge.token gerekli.")
    token = token or secrets.token_urlsafe(48)
    if not foreground and asyncio.run(healthy(token, host)):
        update_env(token)
        print(f"healthOS Codex köprüsü zaten çalışıyor ({host}:{PORT}).")
        if host != "127.0.0.1" and asyncio.run(healthy(token, "127.0.0.1")):
            print(
                "Uyarı: köprü loopback'te de yanıt veriyor; eski bir sürüm tüm arayüzlerde "
                f"dinliyor olabilir. {bridge_home / 'bridge.pid'} içindeki süreci durdurup "
                "bu komutu yeniden çalıştır."
            )
        return
    source_auth = source_home / "auth.json"
    # Separate app-server config avoids loading the desktop's plugins and MCP servers.
    bridge_auth = bridge_home / "auth.json"
    # A service restart must not replace credentials refreshed by the running bridge.
    if not foreground or not bridge_auth.exists():
        if not source_auth.exists():
            raise SystemExit("Önce codex login ile ChatGPT hesabına giriş yap.")
        write_private(bridge_auth, source_auth.read_text(encoding="utf-8"))
    bridge_auth.chmod(0o600)
    write_private(token_file, token)
    bridge_config = 'cli_auth_credentials_store = "file"\nweb_search = "disabled"\n'
    bridge_config += 'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "medium"\n'
    bridge_config += (
        "\n[features]\n"
        + "\n".join(
            f"{feature} = false"
            for feature in (
                "shell_tool",
                "unified_exec",
                "apps",
                "plugins",
                "multi_agent",
                "code_mode",
                "computer_use",
                "browser_use",
                "image_generation",
                "hooks",
            )
        )
        + "\n"
    )
    write_private(bridge_home / "config.toml", bridge_config)
    workspace = bridge_home / "workspace"
    workspace.mkdir(exist_ok=True)
    launcher = shutil.which("codex")
    if not launcher:
        raise SystemExit("Codex CLI bulunamadı. Codex'in kurulu olduğu terminalden çalıştır.")
    if os.name == "nt":
        node = shutil.which("node")
        cli_js = bridge_home / "cli" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        if not cli_js.exists():
            cli_js = (
                Path(launcher).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            )
        if not node or not cli_js.exists():
            raise SystemExit("npm ile kurulu Codex CLI ve Node.js gerekli.")
        command = [node, str(cli_js)]
    else:
        command = [launcher]
    command += [
        "app-server",
        "--listen",
        f"ws://{host}:{PORT}",
        "--ws-auth",
        "capability-token",
        "--ws-token-file",
        str(token_file),
    ]
    child_env = dict(os.environ, CODEX_HOME=str(bridge_home))
    child_env.pop("OPENAI_API_KEY", None)
    if foreground:
        return supervise(command, workspace, child_env)
    with (bridge_home / "bridge.log").open("ab") as log:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
    for _ in range(15):
        if asyncio.run(healthy(token, host)):
            update_env(token)
            (bridge_home / "bridge.pid").write_text(str(process.pid), encoding="utf-8")
            print(f"healthOS Codex köprüsü başlatıldı ({host}:{PORT}, ChatGPT aboneliği).")
            print("Siteyi güncelle: docker compose up -d --build app")
            print(f"Tanılama günlüğü: {bridge_home / 'bridge.log'}")
            return
        if process.poll() is not None:
            break
        time.sleep(0.3)
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    raise SystemExit(f"Codex köprüsü başlatılamadı. Günlük: {bridge_home / 'bridge.log'}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
