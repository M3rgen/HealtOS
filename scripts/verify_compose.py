"""Build and verify an isolated, credential-free Compose stack."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_NAME = f"health-mirror-smoke-{os.getpid()}"
EXPECTED_REVISION = "20261001_0006"
HOST_PORT = "18080"
ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = ROOT / f".compose-smoke-backups-{os.getpid()}"


def find_docker() -> str | None:
    discovered = shutil.which("docker")
    if discovered:
        return discovered
    if os.name == "nt":
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        docker_desktop_cli = (
            program_files / "Docker" / "Docker" / "resources" / "bin" / "docker.exe"
        )
        if docker_desktop_cli.is_file():
            return str(docker_desktop_cli)
    return None


DOCKER_COMMAND = find_docker()


def compose(
    *arguments: str,
    env: dict[str, str],
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    if DOCKER_COMMAND is None:
        raise RuntimeError("Docker CLI is unavailable")
    return subprocess.run(
        [DOCKER_COMMAND, "compose", "--project-name", PROJECT_NAME, *arguments],
        cwd=ROOT,
        env=env,
        check=check,
        text=True,
        capture_output=capture,
    )


def docker(
    *arguments: str,
    env: dict[str, str],
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    if DOCKER_COMMAND is None:
        raise RuntimeError("Docker CLI is unavailable")
    return subprocess.run(
        [DOCKER_COMMAND, *arguments],
        cwd=ROOT,
        env=env,
        check=check,
        text=True,
        capture_output=capture,
    )


def published_ports(service: str, *, env: dict[str, str]) -> dict[str, object]:
    container_id = compose("ps", "--quiet", service, env=env, capture=True).stdout.strip()
    if not container_id:
        raise RuntimeError(f"No running container found for {service}")
    output = docker(
        "inspect",
        "--format",
        "{{json .NetworkSettings.Ports}}",
        container_id,
        env=env,
        capture=True,
    ).stdout
    return json.loads(output)


def main() -> int:
    if DOCKER_COMMAND is None:
        print(
            "Docker is unavailable; install/start Docker Engine or Docker Desktop.", file=sys.stderr
        )
        return 2

    env = os.environ.copy()
    docker_bin = str(Path(DOCKER_COMMAND).parent)
    env.update(
        {
            "APP_ENV_FILE": ".env.example",
            "APP_PORT": HOST_PORT,
            "BACKUP_HOST_DIR": BACKUP_DIR.name,
            "PATH": docker_bin + os.pathsep + env.get("PATH", ""),
            "POSTGRES_PASSWORD": "local-smoke-test-only",
            "RECONCILIATION_ENABLED": "false",
            "WATCHDOG_ENABLED": "false",
        }
    )
    cleanup_needed = False
    try:
        compose("config", "--quiet", env=env)
        cleanup_needed = True
        compose(
            "up",
            "--build",
            "--detach",
            "--wait",
            "--wait-timeout",
            "180",
            "app",
            env=env,
            capture=True,
        )

        revision = compose("exec", "-T", "app", "alembic", "current", env=env, capture=True).stdout
        if EXPECTED_REVISION not in revision:
            raise RuntimeError(f"Expected Alembic revision {EXPECTED_REVISION}; got {revision!r}")

        compose(
            "exec",
            "-T",
            "app",
            "python",
            "-c",
            (
                "import json,urllib.request; "
                "result=json.load(urllib.request.urlopen("
                "'http://127.0.0.1:8000/health',timeout=5)); "
                "assert result['database']=='healthy', result; "
                "assert result['service']=='personal-health-mirror', result; "
                "assert result['scheduler']=='disabled', result"
            ),
            env=env,
        )
        compose("exec", "-T", "app", "health-mirror", "status", env=env)
        compose(
            "exec",
            "-T",
            "app",
            "python",
            "-c",
            (
                "import json,re,urllib.request; "
                "base='http://127.0.0.1:8000'; "
                "html=urllib.request.urlopen(base+'/').read().decode(); "
                "assert 'HealtOS' in html; "
                'assets=re.findall(r\'(?:src|href)="(/assets/[^"]+)"\',html); '
                "assert len(assets)>=2; "
                "assert all(urllib.request.urlopen(base+p).status==200 for p in assets); "
                "data=json.load(urllib.request.urlopen(base+'/api/dashboard')); "
                "assert len(data['days'])==7 and data['days'][0]['steps'] is None; "
                "body=json.dumps({'metric':'muscle','value':42.5,"
                "'measured_at':'2026-09-29T09:00:00Z'}).encode(); "
                "request=urllib.request.Request(base+'/api/dashboard/measurements',data=body,"
                "headers={'Content-Type':'application/json'},method='POST'); "
                "assert urllib.request.urlopen(request).status==201; "
                "saved=json.load(urllib.request.urlopen(base+'/api/dashboard/measurements')); "
                "assert len(saved)==1 and saved[0]['source']=='Manuel'; "
                "data=json.load(urllib.request.urlopen(base+'/api/dashboard')); "
                "assert data['profile']['muscle']['value']==42.5; "
                "csv=urllib.request.urlopen(base+'/api/dashboard/export.csv').read(); "
                "assert b'sleep_seconds' in csv and b'UTC' in csv"
            ),
            env=env,
        )
        app_uid = compose("exec", "-T", "app", "id", "-u", env=env, capture=True).stdout.strip()
        if app_uid != "10001":
            raise RuntimeError(f"Application container has unexpected UID {app_uid!r}")

        app_container_id = compose("ps", "--quiet", "app", env=env, capture=True).stdout.strip()
        app_inspect = json.loads(docker("inspect", app_container_id, env=env, capture=True).stdout)[
            0
        ]
        host_config = app_inspect["HostConfig"]
        if not host_config.get("ReadonlyRootfs"):
            raise RuntimeError("Application container root filesystem is writable")
        if "ALL" not in (host_config.get("CapDrop") or []):
            raise RuntimeError("Application container did not drop all Linux capabilities")
        security_options = host_config.get("SecurityOpt") or []
        if not any(option.split(":", 1)[0] == "no-new-privileges" for option in security_options):
            raise RuntimeError("Application container lacks no-new-privileges")

        compose("--profile", "backup", "run", "--rm", "backup", env=env)
        backup_files = list(BACKUP_DIR.glob("health-*.dump"))
        if len(backup_files) != 1:
            raise RuntimeError(f"Expected one backup artifact; found {len(backup_files)}")
        compose(
            "exec",
            "-T",
            "postgres",
            "createdb",
            "-U",
            "health",
            "health_restore",
            env=env,
        )
        compose(
            "--profile",
            "backup",
            "run",
            "--rm",
            "--entrypoint",
            "pg_restore",
            "backup",
            "--no-owner",
            "--dbname=postgresql://health:local-smoke-test-only@postgres:5432/health_restore",
            f"/backups/{backup_files[0].name}",
            env=env,
        )
        restored = compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "health",
            "-d",
            "health_restore",
            "--tuples-only",
            "--no-align",
            "--command",
            ("SELECT count(*) FROM manual_measurements WHERE metric='muscle' AND value=42.5"),
            env=env,
            capture=True,
        ).stdout.strip()
        if restored != "1":
            raise RuntimeError(f"Backup restore verification returned {restored!r}")

        compose("exec", "-T", "app", "alembic", "downgrade", "base", env=env)
        compose("exec", "-T", "app", "alembic", "upgrade", "head", env=env)
        revision = compose("exec", "-T", "app", "alembic", "current", env=env, capture=True).stdout
        if EXPECTED_REVISION not in revision:
            raise RuntimeError(f"Expected Alembic revision after lifecycle test; got {revision!r}")

        postgres_ports = published_ports("postgres", env=env)
        if postgres_ports.get("5432/tcp") is not None:
            raise RuntimeError(
                f"PostgreSQL unexpectedly has a host binding: {postgres_ports['5432/tcp']}"
            )
        app_bindings = published_ports("app", env=env).get("8000/tcp")
        expected_binding = [{"HostIp": "127.0.0.1", "HostPort": HOST_PORT}]
        if app_bindings != expected_binding:
            raise RuntimeError(f"Unexpected app host binding: {app_bindings!r}")

        print(
            "Compose smoke test passed: build, PostgreSQL, migration, UI assets, dashboard, "
            "manual measurement, CSV, API, CLI, "
            "backup/restore, hardened runtime, and bindings."
        )
        return 0
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"Compose smoke test failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if cleanup_needed:
            compose(
                "down",
                "--volumes",
                "--remove-orphans",
                env=env,
                check=False,
            )
        if BACKUP_DIR.is_dir():
            resolved_root = ROOT.resolve()
            resolved_backup = BACKUP_DIR.resolve()
            if resolved_backup.parent != resolved_root:
                raise RuntimeError("Refusing to remove a backup directory outside the workspace")
            for backup_file in resolved_backup.glob("health-*.dump"):
                backup_file.unlink()
            resolved_backup.rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
