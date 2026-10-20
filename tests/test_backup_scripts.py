"""scripts/backup.sh and scripts/restore.sh against a fake `docker` CLI (no Docker needed)."""

import os
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DUMP = b"PGDMP-synthetic-dump\n"
BASH = shutil.which("bash")
GPG = shutil.which("gpg")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")

FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$*" in
  *"--profile backup run --rm -T backup"*) printf 'PGDMP-synthetic-dump\\n' ;;
  *pg_restore*) cat > "$FAKE_RESTORE_SINK" ;;
  *alembic_version*) echo 20261017_0007 ;;
  *psql*) printf 'manual_measurements 1\\nraw_health_data 0\\n' ;;
esac
"""


@pytest.fixture
def sandbox(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    # gpg-agent's socket path must be short; pytest's macOS tmp_path is too long.
    gnupg = Path(tempfile.mkdtemp(prefix="hos-gpg-", dir="/tmp" if os.path.isdir("/tmp") else None))
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GNUPGHOME": str(gnupg),
        "BACKUP_HOST_DIR": str(tmp_path / "backups"),
        "BACKUP_KEY_FILE": str(tmp_path / "backup.key"),
        "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
        "FAKE_RESTORE_SINK": str(tmp_path / "restored.dump"),
    }
    yield env
    if shutil.which("gpgconf"):
        subprocess.run(["gpgconf", "--kill", "gpg-agent"], env=env, check=False)
    shutil.rmtree(gnupg, ignore_errors=True)


def run(script: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(ROOT / "scripts" / script), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def write_key(env: dict[str, str], mode: int = 0o600) -> Path:
    key = Path(env["BACKUP_KEY_FILE"])
    key.write_text("synthetic-test-passphrase-not-a-secret\n", encoding="utf-8")
    key.chmod(mode)
    return key


@pytest.mark.skipif(GPG is None, reason="gpg is required")
def test_backup_is_encrypted_timestamped_and_prunes_old_dumps(sandbox):
    backups = Path(sandbox["BACKUP_HOST_DIR"])
    backups.mkdir()
    old = time.time() - 20 * 86_400
    stale = [backups / "health-20250101T000000Z.dump.gpg", backups / "health-20250101.dump"]
    keep = [backups / "health-20991231T000000Z.dump.age", backups / "notes.txt"]
    for path in (*stale, *keep):
        path.write_bytes(b"x")
    for path in (*stale, keep[1]):
        os.utime(path, (old, old))
    write_key(sandbox)

    result = run("backup.sh", sandbox)

    assert result.returncode == 0, result.stderr
    output = Path(result.stdout.strip())
    assert output.parent == backups
    assert output.name.startswith("health-") and output.name.endswith("Z.dump.gpg")
    ciphertext = output.read_bytes()
    assert DUMP not in ciphertext and not ciphertext.startswith(b"PGDMP")
    assert stat.S_IMODE(output.stat().st_mode) & 0o077 == 0
    assert not any(path.exists() for path in stale)
    assert all(path.exists() for path in keep)
    assert "--profile backup run --rm -T backup" in Path(sandbox["FAKE_DOCKER_LOG"]).read_text()

    restored = run("restore.sh", sandbox, str(output))

    assert restored.returncode == 0, restored.stderr
    assert Path(sandbox["FAKE_RESTORE_SINK"]).read_bytes() == DUMP
    assert "manual_measurements" in restored.stdout
    log = Path(sandbox["FAKE_DOCKER_LOG"]).read_text()
    assert "createdb -U health health_restore" in log
    assert "--dbname=health_restore" in log


def test_backup_refuses_group_readable_key_file(sandbox):
    write_key(sandbox, 0o640)
    result = run("backup.sh", sandbox)
    assert result.returncode != 0
    assert "chmod 600" in result.stderr
    assert not list(Path(sandbox["BACKUP_HOST_DIR"]).glob("health-*"))


def test_backup_without_key_fails_and_points_away_from_env_file(sandbox):
    result = run("backup.sh", sandbox)
    assert result.returncode != 0
    assert "BACKUP_AGE_RECIPIENT" in result.stderr and "not belong in .env" in result.stderr
    assert not Path(sandbox["FAKE_DOCKER_LOG"]).exists()


def test_restore_refuses_live_database(sandbox, tmp_path: Path):
    dump = tmp_path / "health-20261020T033000Z.dump"
    dump.write_bytes(DUMP)
    result = run("restore.sh", sandbox, str(dump), "health")
    assert result.returncode != 0
    assert "refusing" in result.stderr
    assert not Path(sandbox["FAKE_DOCKER_LOG"]).exists()


def test_systemd_timer_runs_backup_script_daily():
    timer = (ROOT / "deploy" / "systemd" / "healthos-backup.timer").read_text(encoding="utf-8")
    service = (ROOT / "deploy" / "systemd" / "healthos-backup.service").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 03:30:00" in timer and "Persistent=true" in timer
    assert "scripts/backup.sh" in service and "EnvironmentFile=-/etc/healthos/" in service
