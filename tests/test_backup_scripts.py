"""scripts/backup.sh and scripts/restore.sh against a fake `docker` CLI (no Docker needed)."""

import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
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
    printf '%s\\n' "${FAKE_DUMP_TEXT:-PGDMP-synthetic-dump}" ;;
  *pg_restore*) cat > "$FAKE_RESTORE_SINK" ;;
  *alembic_version*) echo 20261017_0007 ;;
  *psql*) printf 'manual_measurements\\t1\\nraw_health_data\\t0\\n' ;;
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
        "BACKUP_STATUS_HOST_DIR": str(tmp_path / "metadata"),
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
    shutil.copy(ROOT / "scripts" / "write_backup_status.py", tree / "scripts")
    (tree / "app" / "services").mkdir(parents=True)
    shutil.copy(ROOT / "app" / "services" / "backup_status.py", tree / "app" / "services")
    shutil.copy(ROOT / "app" / "services" / "backup_files.py", tree / "app" / "services")
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


@pytest.fixture
def fake_age(sandbox):
    """Exercise pipeline completion/failure without a gpg-agent or encryption service."""
    binary = Path(sandbox["PATH"].split(os.pathsep)[0]) / "age"
    binary.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ " $* " == *" --decrypt "* ]]; then\n'
        '  tail -n +2 "${@: -1}" | base64 -d; exit $?\n'
        "fi\n"
        'printf "ciphertext-header\\n"\n'
        "base64\n"
        'if [ -n "${FAKE_ENCRYPT_GATE:-}" ]; then\n'
        '  touch "$FAKE_ENCRYPT_GATE.ready"\n'
        '  while [ ! -e "$FAKE_ENCRYPT_GATE.release" ]; do sleep 0.02; done\n'
        "fi\n"
        '[ -z "${FAKE_ENCRYPT_FAIL:-}" ] || exit 1\n'
    )
    binary.chmod(0o755)
    return {**sandbox, "BACKUP_AGE_RECIPIENT": "synthetic-public-recipient"}


@pytest.fixture
def fixed_age(fake_age):
    binary = Path(fake_age["PATH"].split(os.pathsep)[0]) / "date"
    binary.write_text(
        '#!/usr/bin/env bash\ncase "$*" in\n'
        "  *%Y%m%d*) echo 20261025T033000Z ;;\n"
        "  *) echo 2026-10-25T03:30:00Z ;;\nesac\n"
    )
    binary.chmod(0o755)
    return fake_age


def wait_for(path: Path):
    deadline = time.monotonic() + 10
    while not path.exists():
        assert time.monotonic() < deadline, f"timed out waiting for {path.name}"
        time.sleep(0.02)


@contextmanager
def backup_process(env):
    process = subprocess.Popen(
        [BASH, str(ROOT / "scripts" / "backup.sh")],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)


@pytest.mark.parametrize("method", ["age", "gpg"])
def test_fixed_second_successes_preserve_existing_targets_and_restore(fixed_age, method):
    env = dict(fixed_age)
    binaries = Path(env["PATH"].split(os.pathsep)[0])
    if method == "gpg":
        shutil.copy(binaries / "age", binaries / "gpg")
        env.pop("BACKUP_AGE_RECIPIENT")
    write_key(env)
    backups = Path(env["BACKUP_HOST_DIR"])
    backups.mkdir()
    original = backups / f"health-20261025T033000Z.dump.{method}"
    original.write_bytes(b"ciphertext-header\nUEdETVAtc3ludGhldGljLWR1bXAK\n")
    snapshots = {original: original.read_bytes()}
    for _ in range(2):
        result = run("backup.sh", env)
        assert result.returncode == 0, result.stderr
        output = Path(result.stdout.strip())
        assert output not in snapshots
        assert output in list(backups.glob(f"health-*.dump.{method}"))
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
        snapshots[output] = output.read_bytes()
        assert all(path.read_bytes() == data for path, data in snapshots.items())
    for path in snapshots:
        restored = run("restore.sh", env, str(path))
        assert restored.returncode == 0, restored.stderr
        assert Path(env["FAKE_RESTORE_SINK"]).read_bytes() == DUMP
    # The default free-space guard discovers new names; opt-in retention also does.
    for path in snapshots:
        os.utime(path, (time.time() - 20 * 86400,) * 2)
    result = run("backup.sh", {**env, "BACKUP_RETENTION_DAYS": "7"})
    assert result.returncode == 0, result.stderr
    assert status(env)["pruned_files"] == 3
    assert not any(path.exists() for path in snapshots)
    assert not partials(backups)


def test_concurrent_backups_and_legacy_status_have_private_temporary_files(fixed_age, tmp_path):
    write_key(fixed_age)
    binaries = Path(fixed_age["PATH"].split(os.pathsep)[0])
    real_mv = shutil.which("mv")
    (binaries / "mv").write_text(
        '#!/usr/bin/env bash\nprintf "%s" "$2" > "$FAKE_STATUS_GATE.source"\n'
        'touch "$FAKE_STATUS_GATE.ready"\n'
        'while [ ! -e "$FAKE_STATUS_GATE.release" ]; do sleep 0.02; done\n'
        f'exec "{real_mv}" "$@"\n'
    )
    (binaries / "mv").chmod(0o755)
    envs = [
        {
            **fixed_age,
            "FAKE_ENCRYPT_GATE": str(tmp_path / f"encrypt-{i}"),
            "FAKE_STATUS_GATE": str(tmp_path / f"status-{i}"),
            "FAKE_DUMP_TEXT": f"dump-{i}",
        }
        for i in range(2)
    ]
    backups = Path(fixed_age["BACKUP_HOST_DIR"])
    with backup_process(envs[0]) as first, backup_process(envs[1]) as second:
        for env in envs:
            wait_for(Path(env["FAKE_ENCRYPT_GATE"] + ".ready"))
        assert len(partials(backups)) == 2
        assert not list(backups.glob("health-*.dump.age"))
        for env in envs:
            Path(env["FAKE_ENCRYPT_GATE"] + ".release").touch()
        for env in envs:
            wait_for(Path(env["FAKE_STATUS_GATE"] + ".ready"))
        status_partials = [Path(env["FAKE_STATUS_GATE"] + ".source").read_text() for env in envs]
        assert len(set(status_partials)) == 2
        assert {json.loads(Path(p).read_text())["file"] for p in status_partials} == {
            str(p) for p in backups.glob("health-*.dump.age")
        }
        for env in envs:
            Path(env["FAKE_STATUS_GATE"] + ".release").touch()
        outputs = []
        for process in (first, second):
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stderr
            assert "could not" not in stderr
            outputs.append(Path(stdout.strip()))
        assert len(set(outputs)) == 2
        for i, output in enumerate(outputs):
            assert run("restore.sh", fixed_age, str(output)).returncode == 0
            assert Path(fixed_age["FAKE_RESTORE_SINK"]).read_bytes() == f"dump-{i}\n".encode()
    assert not partials(backups) and not list(backups.glob("*.tmp*"))
    assert status(fixed_age)["file"] in {str(p) for p in outputs}


@pytest.mark.parametrize("failure", ["dump", "encryption", "TERM", "INT"])
def test_failed_or_cancelled_peer_cannot_remove_another_runs_files(fixed_age, tmp_path, failure):
    first = run("backup.sh", fixed_age)
    assert first.returncode == 0, first.stderr
    original = Path(first.stdout.strip())
    before = original.read_bytes()
    env = {**fixed_age, "FAKE_ENCRYPT_GATE": str(tmp_path / "peer")}
    if failure in ("dump", "encryption"):
        env["FAKE_DOCKER_FAIL" if failure == "dump" else "FAKE_ENCRYPT_FAIL"] = "1"
    other = {**fixed_age, "FAKE_ENCRYPT_GATE": str(tmp_path / "survivor")}
    backups = original.parent
    with backup_process(env) as peer, backup_process(other) as survivor:
        wait_for(Path(env["FAKE_ENCRYPT_GATE"] + ".ready"))
        wait_for(Path(other["FAKE_ENCRYPT_GATE"] + ".ready"))
        assert len(partials(backups)) == 2
        if failure in ("TERM", "INT"):
            os.killpg(peer.pid, getattr(signal, "SIG" + failure))
        else:
            Path(env["FAKE_ENCRYPT_GATE"] + ".release").touch()
        stdout, stderr = peer.communicate(timeout=10)
        assert peer.returncode == (143 if failure == "TERM" else 130 if failure == "INT" else 1)
        assert not stdout, stderr
        assert len(partials(backups)) == 1
        assert original.read_bytes() == before
        assert status(fixed_age)["result"] == "failure"
        metadata = json.loads(
            (Path(fixed_age["BACKUP_STATUS_HOST_DIR"]) / "status.json").read_text()
        )
        assert metadata["last_success_at"] == "2026-10-25T03:30:00Z"
        Path(other["FAKE_ENCRYPT_GATE"] + ".release").touch()
        stdout, stderr = survivor.communicate(timeout=10)
        assert survivor.returncode == 0, stderr
        assert Path(stdout.strip()).read_bytes() == before
    assert original.read_bytes() == before and not partials(backups)


def test_publication_error_preserves_output_and_success_evidence(fixed_age):
    first = run("backup.sh", fixed_age)
    assert first.returncode == 0, first.stderr
    original = Path(first.stdout.strip())
    before = original.read_bytes()
    binary = Path(fixed_age["PATH"].split(os.pathsep)[0]) / "python3"
    real_python = shutil.which("python3")
    binary.write_text(
        f'#!/usr/bin/env bash\n[ "${{3:-}}" != publish ] || exit 1\nexec "{real_python}" "$@"\n'
    )
    binary.chmod(0o755)
    result = run("backup.sh", fixed_age)
    assert result.returncode == 1 and result.stdout == ""
    assert "cannot publish" in result.stderr
    assert original.read_bytes() == before
    assert list(original.parent.glob("health-*.dump.age")) == [original]
    assert not partials(original.parent)
    assert status(fixed_age)["result"] == "failure"
    metadata = json.loads((Path(fixed_age["BACKUP_STATUS_HOST_DIR"]) / "status.json").read_text())
    assert metadata["last_success_at"] == "2026-10-25T03:30:00Z"


@pytest.mark.parametrize("failure", ["FAKE_DOCKER_FAIL", "FAKE_ENCRYPT_FAIL"])
def test_shell_success_then_failure_preserves_success_in_separate_metadata(fake_age, failure):
    first = run("backup.sh", fake_age)
    assert first.returncode == 0, first.stderr
    metadata_path = Path(fake_age["BACKUP_STATUS_HOST_DIR"]) / "status.json"
    initial = json.loads(metadata_path.read_text())
    assert initial["last_attempt"]["result"] == "success"
    assert initial["last_success_at"] == status(fake_age)["timestamp"]
    assert Path(first.stdout.strip()).exists()
    failed = run("backup.sh", {**fake_age, failure: "1"})
    assert failed.returncode == 1
    latest = json.loads(metadata_path.read_text())
    assert latest["last_attempt"]["result"] == "failure"
    assert latest["last_success_at"] == initial["last_success_at"]
    assert status(fake_age)["result"] == "failure"  # legacy consumers still work
    assert status(fake_age)["file"] is None
    assert not partials(Path(fake_age["BACKUP_HOST_DIR"]))
    assert Path(first.stdout.strip()).exists()
    for value in (fake_age["BACKUP_HOST_DIR"], fake_age["BACKUP_KEY_FILE"], "message", "file"):
        assert value not in metadata_path.read_text()


@pytest.mark.parametrize("failure", ["FAKE_DOCKER_FAIL", "FAKE_ENCRYPT_FAIL"])
def test_first_incomplete_encrypted_output_cannot_record_success(fake_age, failure):
    result = run("backup.sh", {**fake_age, failure: "1"})
    assert result.returncode == 1
    report = json.loads((Path(fake_age["BACKUP_STATUS_HOST_DIR"]) / "status.json").read_text())
    assert report["last_success_at"] is None
    assert report["last_attempt"]["result"] == "failure"
    assert not list(Path(fake_age["BACKUP_HOST_DIR"]).glob("health-*"))


def test_shell_missing_key_records_failure_without_any_success(sandbox):
    result = run("backup.sh", sandbox)
    assert result.returncode == 1
    metadata = json.loads((Path(sandbox["BACKUP_STATUS_HOST_DIR"]) / "status.json").read_text())
    assert metadata["last_success_at"] is None
    assert metadata["last_attempt"]["result"] == "failure"


def test_shell_legacy_success_is_migrated_before_failure_overwrites_legacy(sandbox):
    backups = Path(sandbox["BACKUP_HOST_DIR"])
    backups.mkdir()
    before = "2026-01-01T00:00:00Z"
    (backups / "last-backup-status.json").write_text(
        json.dumps(
            {
                "result": "success",
                "timestamp": before,
                "file": "/private/archive",
            }
        )
    )
    result = run("backup.sh", sandbox)  # no key -> failure
    assert result.returncode == 1
    metadata = json.loads((Path(sandbox["BACKUP_STATUS_HOST_DIR"]) / "status.json").read_text())
    assert metadata["last_success_at"] == before
    assert metadata["last_attempt"]["result"] == "failure"
    assert status(sandbox)["result"] == "failure"
    assert "/private" not in json.dumps(metadata)


def test_shell_metadata_publication_failure_is_sanitized_and_keeps_legacy(sandbox):
    directory = Path(sandbox["BACKUP_STATUS_HOST_DIR"])
    directory.write_text("not a directory")
    result = run("backup.sh", sandbox)
    assert result.returncode == 1
    assert "could not publish status metadata" in result.stderr
    assert str(directory) not in result.stderr
    assert "Traceback" not in result.stderr
    assert status(sandbox)["result"] == "failure"


@pytest.fixture
def count_docker(sandbox, tmp_path):
    docker = Path(sandbox["PATH"].split(os.pathsep)[0]) / "docker"
    docker.write_text("""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$*" in
  *pg_restore*) cat > "$FAKE_RESTORE_SINK" ;;
  *alembic_version*)
    [ -z "${FAIL_REVISION_QUERY:-}" ] || exit 1
    echo 20261025_0020 ;;
  *psql*)
    case " $* " in
      *" -d health "*)
        [ -z "${FAIL_LIVE_QUERY:-}" ] || { echo "/private/query secret" >&2; exit 1; }
        printf '%s' "$LIVE_COUNTS" ;;
      *)
        [ -z "${FAIL_RESTORED_QUERY:-}" ] || exit 1
        printf '%s' "$RESTORED_COUNTS" ;;
    esac ;;
esac
""")
    docker.chmod(0o755)
    dump = tmp_path / "synthetic.dump"
    dump.write_bytes(DUMP)
    return {**sandbox, "RESTORED_COUNTS": "same\t2\n", "LIVE_COUNTS": "same\t2\n"}, dump


def comparisons(report):
    return {
        fields[0]: fields[1:]
        for fields in (line.split("\t") for line in report.splitlines())
        if len(fields) == 4 and fields[0] != "table"
    }


def test_restore_compares_union_of_tables_without_calling_changes_corruption(count_docker):
    env, dump = count_docker
    env.update(
        {
            "RESTORED_COUNTS": "same\t0\nchanged\t2\nold_table\t3\n",
            "LIVE_COUNTS": "changed\t9\nnew_table\t4\nsame\t0\n",
        }
    )
    result = run("restore.sh", env, str(dump))
    assert result.returncode == 0, result.stderr
    assert comparisons(result.stdout) == {
        "same": ["0", "0", "equal"],
        "changed": ["2", "9", "different"],
        "old_table": ["3", "-", "restored_only"],
        "new_table": ["-", "4", "live_only"],
    }
    assert "differences alone do not imply corruption" in result.stdout
    log = Path(env["FAKE_DOCKER_LOG"]).read_text()
    assert "dropdb -U health --if-exists health_restore" in log
    assert "dropdb -U health --if-exists health\n" not in log
    assert "pg_restore -U health --dbname=health_restore" in log
    assert Path(env["FAKE_RESTORE_SINK"]).read_bytes() == DUMP


@pytest.mark.parametrize("side", ["LIVE", "RESTORED"])
@pytest.mark.parametrize("failure", ["query", "malformed", "duplicate", "negative"])
def test_restore_query_failures_and_invalid_results_never_produce_a_comparison(
    count_docker,
    side,
    failure,
):
    env, dump = count_docker
    if failure == "query":
        env[f"FAIL_{side}_QUERY"] = "1"
    else:
        env[f"{side}_COUNTS"] = {
            "malformed": "same\toops\n",
            "duplicate": "same\t1\nsame\t1\n",
            "negative": "same\t-1\n",
        }[failure]
    result = run("restore.sh", env, str(dump))
    assert result.returncode != 0
    assert not comparisons(result.stdout)
    assert "comparison" not in result.stdout
    assert "/private/query" not in result.stderr


@pytest.mark.parametrize("target", ["health", "postgres", "template0", "template1", "bad;name"])
def test_comparison_changes_keep_scratch_database_guard(count_docker, target):
    env, dump = count_docker
    result = run("restore.sh", env, str(dump), target)
    assert result.returncode != 0
    assert not Path(env["FAKE_DOCKER_LOG"]).exists()


def test_successful_empty_live_query_is_distinct_from_a_query_error(count_docker):
    env, dump = count_docker
    env["LIVE_COUNTS"] = ""
    result = run("restore.sh", env, str(dump))
    assert result.returncode == 0, result.stderr
    assert comparisons(result.stdout) == {"same": ["2", "-", "restored_only"]}


@pytest.mark.parametrize("live", ["same\t2\n", ""])
def test_restore_without_tables_fails_explicitly(count_docker, live):
    env, dump = count_docker
    env.update({"RESTORED_COUNTS": "", "LIVE_COUNTS": live})
    result = run("restore.sh", env, str(dump))
    assert result.returncode != 0
    assert "restored database health_restore has no tables" in result.stderr
    assert "row-count query failed" not in result.stderr
    assert not comparisons(result.stdout) and "comparison" not in result.stdout


def test_large_integer_counts_are_compared_exactly(count_docker):
    env, dump = count_docker
    env.update(
        {"LIVE_COUNTS": "same\t9007199254740993\n", "RESTORED_COUNTS": "same\t9007199254740992\n"}
    )
    result = run("restore.sh", env, str(dump))
    assert result.returncode == 0, result.stderr
    assert comparisons(result.stdout)["same"][2] == "different"


def test_revision_query_error_is_not_a_successful_restore_report(count_docker):
    env, dump = count_docker
    result = run("restore.sh", {**env, "FAIL_REVISION_QUERY": "1"}, str(dump))
    assert result.returncode != 0
    assert "restored revision query failed" in result.stderr
    assert "Alembic revision:" not in result.stdout
