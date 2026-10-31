"""Host-side, stdlib-only atomic publication of sanitized backup metadata."""

from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.backup_status import read_backup_status, utc_timestamp  # noqa: E402


def record(directory: Path, legacy: Path, result: str, timestamp: str) -> None:
    timestamp = utc_timestamp(timestamp)
    if result not in ("success", "failure"):
        raise ValueError("invalid result")
    directory.mkdir(parents=True, exist_ok=True, mode=0o755)
    directory.chmod(0o755)
    path = directory / "status.json"
    with (directory / ".writer.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = read_backup_status(path)
        if previous["status"] == "missing":
            previous = read_backup_status(legacy)
        success = timestamp if result == "success" else previous["last_success_at"]
        data = {
            "schema_version": 1,
            "last_attempt": {"result": result, "timestamp": timestamp},
            "last_success_at": success,
        }
        fd, name = tempfile.mkstemp(prefix=".status-", dir=directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream)
                stream.write("\n")
                stream.flush()
                os.fchmod(stream.fileno(), 0o644)
                os.fsync(stream.fileno())
            os.replace(name, path)
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(name).unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        record(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4])
    except (OSError, ValueError):
        print("backup: could not publish status metadata", file=sys.stderr)
        sys.exit(1)
