"""Build and verify an isolated, credential-free Compose stack."""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_NAME = f"health-mirror-smoke-{os.getpid()}"
EXPECTED_REVISION = "20261020_0010"
HOST_PORT = "18080"
ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = ROOT / f".compose-smoke-backups-{os.getpid()}"
# Matches the `.env*` ignore rules, so it never enters Git or the image build context.
SMOKE_ENV_FILE = ROOT / f".env.compose-smoke-{os.getpid()}"
SMOKE_KEY_FILE = ROOT / f".env.compose-smoke-backup-key-{os.getpid()}"
BASH = shutil.which("bash")


# Runs inside the app container; APP_UI_TOKEN comes from the Compose environment.
UI_SMOKE_SCRIPT = "\n".join(
    [
        "import json, os, re, urllib.error, urllib.request",
        "base = 'http://127.0.0.1:8000'",
        "auth = {'Authorization': 'Bearer ' + os.environ['APP_UI_TOKEN']}",
        "def get(path):",
        "    return urllib.request.urlopen(urllib.request.Request(base + path, headers=auth))",
        "html = get('/').read().decode()",
        "assert 'HealtOS' in html",
        'assets = re.findall(r\'(?:src|href)="(/assets/[^"]+)"\', html)',
        "assert len(assets) >= 2",
        "assert all(get(path).status == 200 for path in assets)",
        "try:",
        "    urllib.request.urlopen(base + '/api/dashboard')",
        "    raise SystemExit('/api/dashboard answered without a bearer token')",
        "except urllib.error.HTTPError as error:",
        "    assert error.code == 401, error.code",
        "data = json.load(get('/api/dashboard'))",
        "assert len(data['days']) == 7 and data['days'][0]['steps'] is None",
        "body = json.dumps({'metric': 'muscle', 'value': 42.5,",
        "                   'measured_at': '2026-09-29T09:00:00Z'}).encode()",
        "headers = {**auth, 'Content-Type': 'application/json', 'X-HealthOS-Request': '1'}",
        "request = urllib.request.Request(base + '/api/dashboard/measurements', data=body,",
        "                                 headers=headers, method='POST')",
        "assert urllib.request.urlopen(request).status == 201",
        "saved = json.load(get('/api/dashboard/measurements'))",
        "assert len(saved) == 1 and saved[0]['source'] == 'Manuel'",
        "data = json.load(get('/api/dashboard'))",
        "assert data['profile']['muscle']['value'] == 42.5",
        "csv = get('/api/dashboard/export.csv').read()",
        "assert b'sleep_seconds' in csv and b'UTC' in csv",
    ]
)


def backup_key_env() -> dict[str, str]:
    """A throwaway key for the encrypted backup/restore round trip (age, else gpg)."""
    SMOKE_KEY_FILE.unlink(missing_ok=True)
    if shutil.which("age") and shutil.which("age-keygen"):
        subprocess.run(["age-keygen", "-o", str(SMOKE_KEY_FILE)], check=True, capture_output=True)
    elif shutil.which("gpg"):
        SMOKE_KEY_FILE.write_text(secrets.token_urlsafe(48) + "\n", encoding="utf-8")
    else:
        raise RuntimeError("Encrypted backup verification needs age or gpg on the host")
    SMOKE_KEY_FILE.chmod(0o600)
    return {"BACKUP_KEY_FILE": str(SMOKE_KEY_FILE), "COMPOSE_PROJECT_NAME": PROJECT_NAME}


def write_smoke_env_file() -> None:
    """Synthetic, non-placeholder values so the production guard (APP_ENV) passes."""
    overrides = {
        "GOOGLE_CLIENT_ID": "smoke-test-client-id",
        "GOOGLE_CLIENT_SECRET": secrets.token_urlsafe(24),
        "GOOGLE_TOKEN_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI": "https://webhook.example.com/webhooks/google-health",
        "GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION": f"Bearer {secrets.token_urlsafe(24)}",
    }
    lines = [
        line
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line.partition("=")[0].strip() not in overrides
    ]
    lines += [f"{name}={value}" for name, value in overrides.items()]
    SMOKE_ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    if BASH is None:
        print("bash is required for scripts/backup.sh and scripts/restore.sh.", file=sys.stderr)
        return 2

    env = os.environ.copy()
    docker_bin = str(Path(DOCKER_COMMAND).parent)
    env.update(
        {
            "APP_ENV_FILE": SMOKE_ENV_FILE.name,
            "APP_PORT": HOST_PORT,
            # Throwaway UI bearer token for the isolated stack (required in production).
            "APP_UI_TOKEN": secrets.token_urlsafe(32),
            "BACKUP_HOST_DIR": BACKUP_DIR.name,
            "PATH": docker_bin + os.pathsep + env.get("PATH", ""),
            "POSTGRES_PASSWORD": "local-smoke-test-only",
            "RECONCILIATION_ENABLED": "false",
            "WATCHDOG_ENABLED": "false",
        }
    )
    cleanup_needed = False
    try:
        write_smoke_env_file()
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
            UI_SMOKE_SCRIPT,
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

        postgres_container_id = compose(
            "ps", "--quiet", "postgres", env=env, capture=True
        ).stdout.strip()
        postgres_host_config = json.loads(
            docker("inspect", postgres_container_id, env=env, capture=True).stdout
        )[0]["HostConfig"]
        if "ALL" not in (postgres_host_config.get("CapDrop") or []):
            raise RuntimeError("PostgreSQL container did not drop all Linux capabilities")

        backup_env = {**env, **backup_key_env()}
        backup_path = Path(
            subprocess.run(
                [BASH, str(ROOT / "scripts" / "backup.sh")],
                cwd=ROOT,
                env=backup_env,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
        )
        backup_files = sorted(BACKUP_DIR.glob("health-*.dump*"))
        if backup_files != [ROOT / backup_path] or backup_path.suffix not in {".age", ".gpg"}:
            raise RuntimeError(f"Expected one encrypted backup artifact; found {backup_files}")
        if (ROOT / backup_path).read_bytes()[:5] == b"PGDMP":
            raise RuntimeError("Backup artifact is an unencrypted pg_dump archive")
        restore_report = subprocess.run(
            [BASH, str(ROOT / "scripts" / "restore.sh"), str(backup_path), "health_restore"],
            cwd=ROOT,
            env=backup_env,
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        counts = {
            fields[0]: fields[1]
            for fields in (line.split() for line in restore_report.splitlines())
            if len(fields) == 3
        }
        if counts.get("manual_measurements") != "1" or counts.get("raw_health_data") != "0":
            raise RuntimeError(f"Unexpected restored row counts: {restore_report!r}")
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
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr, file=sys.stderr)
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
        SMOKE_ENV_FILE.unlink(missing_ok=True)
        SMOKE_KEY_FILE.unlink(missing_ok=True)
        if BACKUP_DIR.is_dir():
            resolved_root = ROOT.resolve()
            resolved_backup = BACKUP_DIR.resolve()
            if resolved_backup.parent != resolved_root:
                raise RuntimeError("Refusing to remove a backup directory outside the workspace")
            for backup_file in resolved_backup.glob("health-*.dump*"):
                backup_file.unlink()
            resolved_backup.rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
