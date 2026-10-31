"""Read only backup evidence; never expose private artifact details or OS errors."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict


class Attempt(TypedDict):
    result: str
    timestamp: str


class BackupStatus(TypedDict):
    status: str
    last_attempt: Attempt | None
    last_success_at: str | None


def utc_timestamp(value: object) -> str:
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("timezone required")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def read_backup_status(path: Path | None) -> BackupStatus:
    empty: BackupStatus = {"status": "unconfigured", "last_attempt": None, "last_success_at": None}
    if path is None:
        return empty
    try:
        with path.open("rb") as stream:
            raw = stream.read(65_537)
        if len(raw) > 65_536:
            raise ValueError("oversize metadata")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("object required")
        legacy = "schema_version" not in data
        if not legacy and (type(data["schema_version"]) is not int or data["schema_version"] != 1):
            raise ValueError("unsupported schema")
        attempt = data if legacy else data["last_attempt"]
        result = attempt["result"]
        if result not in ("success", "failure"):
            raise ValueError("invalid result")
        timestamp = utc_timestamp(attempt["timestamp"])
        success = (
            data.get("last_success_at")
            if not legacy
            else (timestamp if result == "success" else None)
        )
        if not legacy and "last_success_at" not in data:
            raise ValueError("missing success evidence")
        if success is not None:
            success = utc_timestamp(success)
            if datetime.fromisoformat(success) > datetime.fromisoformat(timestamp):
                raise ValueError("success after attempt")
        if result == "success" and success != timestamp:
            raise ValueError("inconsistent success")
        return {
            "status": "legacy" if legacy else "available",
            "last_attempt": {"result": result, "timestamp": timestamp},
            "last_success_at": success,
        }
    except FileNotFoundError:
        empty["status"] = "missing"
    except OSError:
        empty["status"] = "unreadable"
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        empty["status"] = "invalid"
    return empty
