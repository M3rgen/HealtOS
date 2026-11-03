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

PROJECT_NAME = f"health-mirror-smoke-{os.getpid()}-{secrets.token_hex(4)}"
EXPECTED_REVISION = "20261102_0023"
HOST_PORT = "18080"
ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = ROOT / f".compose-smoke-backups-{os.getpid()}"
STATUS_DIR = ROOT / f".compose-smoke-status-{os.getpid()}"
# Matches the `.env*` ignore rules, so it never enters Git or the image build context.
SMOKE_ENV_FILE = ROOT / f".env.compose-smoke-{os.getpid()}"
SMOKE_KEY_FILE = ROOT / f".env.compose-smoke-backup-key-{os.getpid()}"
BASH = shutil.which("bash")


# Both smoke scripts run inside the app container; APP_UI_TOKEN (the throwaway token set in
# main()) comes from the Compose environment. Every route except /health/live and the login,
# webhook and OAuth-callback routes needs it, so each request sends it; the explicit
# "no token -> 401" probes are the only exceptions.
HEALTH_SMOKE_SCRIPT = "\n".join(
    [
        "import json, os, urllib.error, urllib.request",
        "base = 'http://127.0.0.1:8000'",
        "auth = {'Authorization': 'Bearer ' + os.environ['APP_UI_TOKEN']}",
        "assert urllib.request.urlopen(base + '/health/live', timeout=5).status == 200",
        "try:",
        "    urllib.request.urlopen(base + '/health', timeout=5)",
        "    raise SystemExit('/health answered without a bearer token')",
        "except urllib.error.HTTPError as error:",
        "    assert error.code == 401, error.code",
        "request = urllib.request.Request(base + '/health', headers=auth)",
        "result = json.load(urllib.request.urlopen(request, timeout=5))",
        "assert result['database'] == 'healthy', result",
        "assert result['service'] == 'personal-health-mirror', result",
        # The flags below switch off reconciliation, the watchdog and the identity reconciler,
        # but the derived-day refresh always runs, so the scheduler must be up.
        "assert result['scheduler'] == 'running', result",
    ]
)

UI_SMOKE_SCRIPT = "\n".join(
    [
        "import json, os, re, urllib.error, urllib.request",
        "base = 'http://127.0.0.1:8000'",
        "auth = {'Authorization': 'Bearer ' + os.environ['APP_UI_TOKEN']}",
        "def get(path):",
        "    return urllib.request.urlopen(urllib.request.Request(base + path, headers=auth))",
        "html = get('/').read().decode()",
        "assert '<title>HealthOS' in html, html[:200]",
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


def verify_runtime(service: str, inspection: dict) -> None:
    """Check actual Engine settings, including long-syntax tmpfs mounts."""
    config = inspection["HostConfig"]
    if config.get("ReadonlyRootfs") is not True:
        raise RuntimeError(f"{service}: container root filesystem is writable")
    if "ALL" not in (config.get("CapDrop") or []):
        raise RuntimeError(f"{service}: container did not drop all Linux capabilities")
    options = [
        option.replace("=", ":").split(":", 1)
        for option in config.get("SecurityOpt") or []
        if option.replace("=", ":").split(":", 1)[0] == "no-new-privileges"
    ]
    if not options or any(len(option) > 1 and option[1] != "true" for option in options):
        raise RuntimeError(f"{service}: container lacks enabled no-new-privileges")
    temporary = ["/tmp", "/var/run/postgresql"] if service == "postgres" else ["/tmp"]
    mounts = config.get("Mounts") or []
    for target in temporary:
        short = (config.get("Tmpfs") or {}).get(target)
        long = [mount for mount in mounts if mount.get("Target") == target]
        if short is not None:
            if "ro" not in short.split(","):
                continue
        elif len(long) == 1 and long[0].get("Type") == "tmpfs" and not long[0].get("ReadOnly"):
            continue
        raise RuntimeError(f"{service}: missing writable tmpfs at {target}")
    if service == "postgres":
        data = [
            mount
            for mount in inspection.get("Mounts", [])
            if mount.get("Destination") == "/var/lib/postgresql/data"
        ]
        if len(data) != 1 or not (
            data[0].get("Type") == "volume"
            and data[0].get("RW") is True
            and data[0].get("Name") == f"{PROJECT_NAME}_postgres_data"
        ):
            raise RuntimeError(
                "postgres: data must use the isolated project's writable named volume"
            )


def check_running_hardening(*, env: dict[str, str]) -> None:
    for service in ("app", "postgres"):
        container_id = compose("ps", "--quiet", service, env=env, capture=True).stdout.strip()
        if not container_id:
            raise RuntimeError(f"No running container found for {service}")
        inspection = json.loads(docker("inspect", container_id, env=env, capture=True).stdout)[0]
        verify_runtime(service, inspection)
        # Exercise the permissions as the actual application/database user; a root
        # exec would hide a bad tmpfs mode. PID 1 must already have dropped root.
        if service == "postgres":
            compose(
                "exec",
                "-T",
                "--user",
                "postgres",
                service,
                "sh",
                "-ec",
                'test "$(awk \'/^Uid:/ {print $2}\' /proc/1/status)" = "$(id -u)"; '
                "for dir in /tmp /var/run/postgresql; do "
                'probe=$(mktemp "$dir/healthos-smoke.XXXXXX"); rm "$probe"; done',
                env=env,
            )
        else:
            compose(
                "exec",
                "-T",
                service,
                "python",
                "-c",
                "import tempfile; tempfile.TemporaryFile(dir='/tmp').close()",
                env=env,
            )


def check_persisted_measurement(*, env: dict[str, str]) -> None:
    rows = compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "health",
        "-d",
        "health",
        "--tuples-only",
        "--no-align",
        "--set",
        "ON_ERROR_STOP=1",
        "--command",
        "SELECT count(*) FROM manual_measurements WHERE metric='muscle' AND value=42.5",
        env=env,
        capture=True,
    ).stdout.strip()
    if rows != "1":
        raise RuntimeError(f"postgres: synthetic measurement did not persist: {rows!r}")


def verify_postgres_lifecycle(*, env: dict[str, str]) -> None:
    # First exercise a process restart, then the entrypoint on an existing data
    # directory in a fresh container. Neither operation removes the named volume.
    compose("restart", "postgres", env=env, capture=True)
    compose("up", "--detach", "--wait", "--wait-timeout", "180", "app", env=env, capture=True)
    check_persisted_measurement(env=env)
    check_running_hardening(env=env)
    compose(
        "up",
        "--detach",
        "--force-recreate",
        "--wait",
        "--wait-timeout",
        "180",
        "postgres",
        "app",
        env=env,
        capture=True,
    )
    check_persisted_measurement(env=env)
    check_running_hardening(env=env)
    compose("exec", "-T", "app", "python", "-c", HEALTH_SMOKE_SCRIPT, env=env)


def verify_restore_counts(report: str, tables: set[str]) -> None:
    counts = {}
    try:
        rows = report.split("table\trestored\tlive\tcomparison\n", 1)[1].split(
            "\nCounts compare", 1
        )[0]
    except IndexError:
        raise RuntimeError("Missing restore comparison report") from None
    for line in rows.splitlines():
        fields = line.split("\t")
        if len(fields) != 4:
            raise RuntimeError("Invalid restore comparison row")
        table, restored, live, comparison = fields
        if table in counts or not restored.isdecimal() or restored != live or comparison != "equal":
            raise RuntimeError("Controlled backup drill has unequal or invalid table counts")
        counts[table] = restored
    if set(counts) != tables:
        raise RuntimeError("Controlled backup drill did not compare every table")
    if counts.get("manual_measurements") != "1" or counts.get("raw_health_data") != "0":
        raise RuntimeError("Controlled backup drill is missing expected synthetic tables")


def check_backup_health(expected: dict, *, env: dict[str, str]) -> None:
    script = "\n".join(
        [
            "import json, os, urllib.error, urllib.request",
            "url = 'http://127.0.0.1:8000/health/backup'",
            "try:",
            "    urllib.request.urlopen(url, timeout=5)",
            "    raise SystemExit('Backup health accepted an anonymous request')",
            "except urllib.error.HTTPError as error:",
            "    assert error.code == 401",
            "headers = {'Authorization': 'Bearer ' + os.environ['APP_UI_TOKEN']}",
            "request = urllib.request.Request(url, headers=headers)",
            "result = json.load(urllib.request.urlopen(request, timeout=5))",
            f"assert result == json.loads({json.dumps(expected)!r}), 'Backup metadata mismatch'",
        ]
    )
    compose("exec", "-T", "app", "python", "-c", script, env=env)


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
            # Pin the file for this process AND backup/restore subprocesses;
            # inherited development overrides must never select another database.
            "COMPOSE_FILE": str(ROOT / "docker-compose.yml"),
            "COMPOSE_PROFILES": "",
            "APP_ENV_FILE": SMOKE_ENV_FILE.name,
            "APP_PORT": HOST_PORT,
            # Throwaway UI bearer token for the isolated stack (required in production).
            "APP_UI_TOKEN": secrets.token_urlsafe(32),
            "BACKUP_HOST_DIR": BACKUP_DIR.name,
            "BACKUP_STATUS_HOST_DIR": str(STATUS_DIR),
            # A throwaway database; do not demand the production 5 GiB headroom.
            "BACKUP_MIN_FREE_MB": "64",
            "PATH": docker_bin + os.pathsep + env.get("PATH", ""),
            "POSTGRES_PASSWORD": "local-smoke-test-only",
            "RECONCILIATION_ENABLED": "false",
            "WATCHDOG_ENABLED": "false",
            "IDENTITY_RECONCILE_ENABLED": "false",
        }
    )
    cleanup_needed = False
    try:
        write_smoke_env_file()
        compose("config", "--quiet", env=env)
        STATUS_DIR.mkdir(mode=0o755)  # host owns it; Docker must not create it as root
        STATUS_DIR.chmod(0o755)
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
            HEALTH_SMOKE_SCRIPT,
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

        check_running_hardening(env=env)
        verify_postgres_lifecycle(env=env)
        check_backup_health(
            {"status": "missing", "last_attempt": None, "last_success_at": None}, env=env
        )
        # Freeze the synthetic archive during dump and comparison. All schedulers
        # are disabled; stopping app also excludes startup/HTTP background writes.
        compose("stop", "app", env=env, capture=True)
        tables = set(
            compose(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "health",
                "-d",
                "health",
                "--tuples-only",
                "--no-align",
                "--set",
                "ON_ERROR_STOP=1",
                "--command",
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE'",
                env=env,
                capture=True,
            ).stdout.splitlines()
        )

        backup_env = {
            **env,
            **backup_key_env(),
            "BACKUP_AGE_RECIPIENT": "",
            "BACKUP_AGE_IDENTITY": str(SMOKE_KEY_FILE),
            "BACKUP_RETENTION_DAYS": "never",
        }
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
        backup_status = json.loads((BACKUP_DIR / "last-backup-status.json").read_text("utf-8"))
        if backup_status.get("result") != "success":
            raise RuntimeError(f"Backup status file does not report success: {backup_status}")
        restore_report = subprocess.run(
            [BASH, str(ROOT / "scripts" / "restore.sh"), str(backup_path), "health_restore"],
            cwd=ROOT,
            env=backup_env,
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        verify_restore_counts(restore_report, tables)
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

        compose("up", "--detach", "--wait", "app", env=env, capture=True)
        metadata = json.loads((STATUS_DIR / "status.json").read_text("utf-8"))
        expected = {key: metadata[key] for key in ("last_attempt", "last_success_at")}
        expected["status"] = "available"
        check_backup_health(expected, env=env)
        failed = subprocess.run(
            [BASH, str(ROOT / "scripts" / "backup.sh")],
            cwd=ROOT,
            env={**backup_env, "BACKUP_MIN_FREE_MB": "invalid"},
            text=True,
            capture_output=True,
            check=False,
        )
        if failed.returncode == 0:
            raise RuntimeError("Backup failure probe unexpectedly succeeded")
        latest = json.loads((STATUS_DIR / "status.json").read_text("utf-8"))
        if (
            latest["last_attempt"]["result"] != "failure"
            or latest["last_success_at"] != expected["last_success_at"]
        ):
            raise RuntimeError("Backup failure lost last success evidence")
        expected["last_attempt"] = latest["last_attempt"]
        check_backup_health(expected, env=env)  # atomic replacement visible before recreation
        compose("up", "--detach", "--force-recreate", "--wait", "app", env=env, capture=True)
        check_backup_health(expected, env=env)

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
            "Compose smoke test passed: build, PostgreSQL, migration, health (token required), "
            "UI assets, dashboard, "
            "manual measurement, CSV, API, CLI, "
            "PostgreSQL restart/recreation persistence, backup/restore, "
            "hardened runtime, and bindings."
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
        if STATUS_DIR.is_dir():
            if STATUS_DIR.resolve().parent != ROOT.resolve():
                raise RuntimeError("Refusing to remove status directory outside the workspace")
            for name in ("status.json", ".writer.lock"):
                (STATUS_DIR / name).unlink(missing_ok=True)
            STATUS_DIR.rmdir()
        if BACKUP_DIR.is_dir():
            resolved_root = ROOT.resolve()
            resolved_backup = BACKUP_DIR.resolve()
            if resolved_backup.parent != resolved_root:
                raise RuntimeError("Refusing to remove a backup directory outside the workspace")
            for backup_file in resolved_backup.glob("health-*.dump*"):
                backup_file.unlink()
            (resolved_backup / "last-backup-status.json").unlink(missing_ok=True)
            # backup.sh status temporaries: .last-backup-status.json.tmp.XXXXXXXX
            # (and the older fixed .last-backup-status.json.tmp).
            for status_file in resolved_backup.glob(".last-backup-status.json.tmp*"):
                status_file.unlink()
            resolved_backup.rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
