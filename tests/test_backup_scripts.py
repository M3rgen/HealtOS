"""scripts/backup.sh and scripts/restore.sh against a fake `docker` CLI (no Docker needed)."""

import json
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
GIB = 1024**3
needs_gpg = pytest.mark.skipif(GPG is None, reason="gpg is required")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")

FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$*" in
  *"--profile backup run --rm -T backup"*)
    [ -z "${FAKE_DOCKER_FAIL:-}" ] || exit 1
    printf 'PGDMP-synthetic-dump\\n' ;;
  *pg_restore*) cat > "$FAKE_RESTORE_SINK" ;;
  *alembic_version*) echo 20261017_0007 ;;
  *psql*) printf 'manual_measurements 1\\nraw_health_data 0\\n' ;;
esac
"""

# Real df unless FAKE_DF_AVAIL_KB is set; then a POSIX `df -Pk` line with that "Available".
FAKE_DF = """#!/usr/bin/env bash
if [ -n "${FAKE_DF_AVAIL_KB:-}" ]; then
  echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
  echo "fake 999999999 1 $FAKE_DF_AVAIL_KB 1% /"
  exit 0
fi
exec /bin/df "$@"
"""


@pytest.fixture
def sandbox(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    df = bin_dir / "df"
    df.write_text(FAKE_DF, encoding="utf-8")
    df.chmod(0o755)
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
        # 100 GiB free unless a test says otherwise, independent of the real disk.
        "FAKE_DF_AVAIL_KB": str(100 * 1024 * 1024),
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


def status(env: dict[str, str]) -> dict:
    path = Path(env["BACKUP_HOST_DIR"]) / "last-backup-status.json"
    return json.loads(path.read_text(encoding="utf-8"))


def old_dumps(backups: Path, days: int = 20) -> tuple[list[Path], list[Path]]:
    """Old health-* dumps (prunable) and old non-dump files (never prunable)."""
    backups.mkdir(exist_ok=True)
    old = time.time() - days * 86_400
    dumps = [
        backups / "health-20250101T000000Z.dump.gpg",
        backups / "health-20250102T000000Z.dump.age",
        backups / "health-20250101.dump",
    ]
    others = [backups / "notes.txt", backups / "other-20250101.dump"]
    for path in (*dumps, *others):
        path.write_bytes(b"x")
        os.utime(path, (old, old))
    return dumps, others


def partials(backups: Path) -> list[Path]:
    return list(backups.glob("*.partial"))


@needs_gpg
def test_backup_is_encrypted_timestamped_and_keeps_old_dumps_by_default(sandbox):
    backups = Path(sandbox["BACKUP_HOST_DIR"])
    dumps, others = old_dumps(backups)
    write_key(sandbox)

    result = run("backup.sh", sandbox)

    assert result.returncode == 0, result.stderr
    output = Path(result.stdout.strip())
    assert output.parent == backups
    assert output.name.startswith("health-") and output.name.endswith("Z.dump.gpg")
    ciphertext = output.read_bytes()
    assert DUMP not in ciphertext and not ciphertext.startswith(b"PGDMP")
    assert stat.S_IMODE(output.stat().st_mode) & 0o077 == 0
    assert all(path.exists() for path in (*dumps, *others))
    assert "free space in" in result.stderr
    assert "--profile backup run --rm -T backup" in Path(sandbox["FAKE_DOCKER_LOG"]).read_text()
    report = status(sandbox)
    assert report["result"] == "success"
    assert report["file"] == str(output)
    assert report["size_bytes"] == len(ciphertext)
    assert report["free_bytes"] == 100 * GIB
    assert report["retention_days"] == "never" and report["pruned_files"] == 0

    restored = run("restore.sh", sandbox, str(output))

    assert restored.returncode == 0, restored.stderr
    assert Path(sandbox["FAKE_RESTORE_SINK"]).read_bytes() == DUMP
    assert "manual_measurements" in restored.stdout
    log = Path(sandbox["FAKE_DOCKER_LOG"]).read_text()
    assert "createdb -U health health_restore" in log
    assert "--dbname=health_restore" in log


@needs_gpg
@pytest.mark.parametrize("value", ["", "0", "never", "NEVER", "36500"])
def test_unset_zero_never_and_36500_retention_delete_nothing(sandbox, value):
    backups = Path(sandbox["BACKUP_HOST_DIR"])
    dumps, others = old_dumps(backups, days=400)
    write_key(sandbox)

    result = run("backup.sh", {**sandbox, "BACKUP_RETENTION_DAYS": value})

    assert result.returncode == 0, result.stderr
    assert all(path.exists() for path in (*dumps, *others))
    assert len(list(backups.glob("health-*.dump*"))) == len(dumps) + 1
    assert status(sandbox)["pruned_files"] == 0


def run_with_env_file(
    tmp_path: Path, env: dict[str, str], content: str
) -> subprocess.CompletedProcess[str]:
    # .env lives in the repository root; simulate it through a copy of the script tree.
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "backup.sh", tree / "scripts" / "backup.sh")
    (tree / ".env").write_text(content, encoding="utf-8")
    return subprocess.run(
        [BASH, str(tree / "scripts" / "backup.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@needs_gpg
def test_retention_from_env_file_36500_is_accepted(sandbox, tmp_path: Path):
    backups = Path(sandbox["BACKUP_HOST_DIR"])
    dumps, _ = old_dumps(backups, days=400)
    write_key(sandbox)

    result = run_with_env_file(tmp_path, sandbox, "BACKUP_RETENTION_DAYS=36500\n")

    assert result.returncode == 0, result.stderr
    assert all(path.exists() for path in dumps)
    assert status(sandbox)["retention_days"] == "36500"


@needs_gpg
def test_a_positive_retention_in_env_file_prunes(sandbox, tmp_path: Path):
    backups = Path(sandbox["BACKUP_HOST_DIR"])
    dumps, others = old_dumps(backups)
    write_key(sandbox)

    result = run_with_env_file(tmp_path, sandbox, "BACKUP_RETENTION_DAYS=7\n")

    assert result.returncode == 0, result.stderr
    assert not any(path.exists() for path in dumps)
    assert all(path.exists() for path in others)
    assert status(sandbox)["retention_days"] == "7"


@needs_gpg
@pytest.mark.parametrize("value", ["", "0", "never"])
def test_an_explicit_empty_or_never_environment_value_overrides_env_file(
    sandbox, tmp_path: Path, value
):
    # An empty BACKUP_RETENTION_DAYS in the environment used to fall back to .env's 7.
    backups = Path(sandbox["BACKUP_HOST_DIR"])
    dumps, others = old_dumps(backups)
    write_key(sandbox)

    result = run_with_env_file(
        tmp_path, {**sandbox, "BACKUP_RETENTION_DAYS": value}, "BACKUP_RETENTION_DAYS=7\n"
    )

    assert result.returncode == 0, result.stderr
    assert all(path.exists() for path in (*dumps, *others))
    report = status(sandbox)
    assert report["retention_days"] == "never" and report["pruned_files"] == 0


@needs_gpg
def test_explicit_positive_retention_prunes_only_old_health_dumps(sandbox):
    backups = Path(sandbox["BACKUP_HOST_DIR"])
    dumps, others = old_dumps(backups)
    recent = backups / "health-20991231T000000Z.dump.age"
    recent.write_bytes(b"x")
    write_key(sandbox)

    result = run("backup.sh", {**sandbox, "BACKUP_RETENTION_DAYS": "7"})

    assert result.returncode == 0, result.stderr
    output = Path(result.stdout.strip())
    assert output.exists() and recent.exists()
    assert not any(path.exists() for path in dumps)
    assert all(path.exists() for path in others)
    report = status(sandbox)
    assert report["retention_days"] == "7" and report["pruned_files"] == len(dumps)


@needs_gpg
@pytest.mark.parametrize("value", ["-5", "abc", "7d", " 7", "1e3", "99999999999999999999"])
def test_any_other_retention_value_keeps_every_backup(sandbox, value):
    # Neither deletes nor stops the backup: a typo must not cost a night's backup.
    backups = Path(sandbox["BACKUP_HOST_DIR"])
    dumps, others = old_dumps(backups)
    write_key(sandbox)

    result = run("backup.sh", {**sandbox, "BACKUP_RETENTION_DAYS": value})

    assert result.returncode == 0, result.stderr
    assert all(path.exists() for path in (*dumps, *others))
    report = status(sandbox)
    assert report["result"] == "success"
    assert report["retention_days"] == "never" and report["pruned_files"] == 0
    if len(value) <= 9:
        assert "keeping every backup" in result.stderr


def test_low_disk_refuses_to_start_writes_nothing_and_deletes_nothing(sandbox):
    backups = Path(sandbox["BACKUP_HOST_DIR"])
    dumps, others = old_dumps(backups)
    write_key(sandbox)
    env = {**sandbox, "FAKE_DF_AVAIL_KB": str(512 * 1024), "BACKUP_RETENTION_DAYS": "7"}

    result = run("backup.sh", env)

    assert result.returncode != 0
    assert "refusing to start" in result.stderr
    assert "No backup was written or deleted" in result.stderr
    assert result.stdout == ""
    assert not Path(sandbox["FAKE_DOCKER_LOG"]).exists()
    assert all(path.exists() for path in (*dumps, *others))
    assert len(list(backups.glob("health-*"))) == len(dumps)
    assert not partials(backups)
    report = status(sandbox)
    assert report["result"] == "failure" and "refusing to start" in report["message"]
    assert report["file"] is None
    assert report["free_bytes"] == GIB // 2 and report["required_free_bytes"] == GIB


def test_first_backup_needs_five_gib_by_default(sandbox):
    write_key(sandbox)
    result = run("backup.sh", {**sandbox, "FAKE_DF_AVAIL_KB": str(4 * 1024 * 1024)})
    assert result.returncode != 0
    assert "5 GiB before the first dump" in result.stderr
    assert status(sandbox)["required_free_bytes"] == 5 * GIB


def test_free_space_default_is_twice_the_newest_dump(sandbox):
    backups = Path(sandbox["BACKUP_HOST_DIR"])
    backups.mkdir()
    older = backups / "health-20250101T000000Z.dump.gpg"
    older.write_bytes(b"x")
    os.utime(older, (time.time() - 86_400,) * 2)
    newest = backups / "health-20250102T000000Z.dump.gpg"
    with newest.open("wb") as handle:  # sparse: 3 GiB apparent size, no real disk use
        handle.truncate(3 * GIB)
    write_key(sandbox)

    result = run("backup.sh", {**sandbox, "FAKE_DF_AVAIL_KB": str(5 * 1024 * 1024)})

    assert result.returncode != 0
    assert "twice the newest dump" in result.stderr
    assert status(sandbox)["required_free_bytes"] == 6 * GIB
    assert older.exists() and newest.exists() and not partials(backups)


@needs_gpg
def test_min_free_override_allows_a_small_disk(sandbox):
    write_key(sandbox)
    env = {**sandbox, "FAKE_DF_AVAIL_KB": "200000", "BACKUP_MIN_FREE_MB": "100"}
    result = run("backup.sh", env)
    assert result.returncode == 0, result.stderr
    assert status(sandbox)["required_free_bytes"] == 100 * 1024 * 1024


@needs_gpg
def test_failed_dump_leaves_no_partial_and_records_failure(sandbox):
    backups = Path(sandbox["BACKUP_HOST_DIR"])
    dumps, others = old_dumps(backups)
    write_key(sandbox)

    result = run("backup.sh", {**sandbox, "FAKE_DOCKER_FAIL": "1", "BACKUP_RETENTION_DAYS": "7"})

    assert result.returncode != 0
    assert "no backup was written" in result.stderr
    assert result.stdout == ""
    assert all(path.exists() for path in (*dumps, *others))
    assert len(list(backups.glob("health-*"))) == len(dumps)
    assert not partials(backups)
    report = status(sandbox)
    assert report["result"] == "failure" and report["file"] is None


def test_backup_refuses_group_readable_key_file(sandbox):
    write_key(sandbox, 0o640)
    result = run("backup.sh", sandbox)
    assert result.returncode != 0
    assert "chmod 600" in result.stderr
    assert not list(Path(sandbox["BACKUP_HOST_DIR"]).glob("health-*"))
    report = status(sandbox)
    assert report["result"] == "failure" and "chmod 600" in report["message"]


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
    assert "OnFailure=healthos-backup-failure.service" in service
    failure = (ROOT / "deploy" / "systemd" / "healthos-backup-failure.service").read_text(
        encoding="utf-8"
    )
    assert "logger" in failure and "user.crit" in failure
