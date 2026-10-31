"""Backup evidence is durable, bounded, explicit, and safe to return over HTTP."""

import json
import stat
from pathlib import Path

import httpx
import pytest
from test_auth import BASE, TOKEN, build

from app.services.backup_status import read_backup_status
from scripts import write_backup_status as writer

FIRST = "2026-10-24T01:02:03Z"
SECOND = "2026-10-25T01:02:03Z"


def document(result="failure", success=FIRST, timestamp=SECOND):
    return {
        "schema_version": 1,
        "last_attempt": {"result": result, "timestamp": timestamp},
        "last_success_at": success,
    }


def save(tmp_path, data):
    path = tmp_path / "status.json"
    path.write_text(json.dumps(data))
    return path


@pytest.mark.parametrize(
    "result,success", [("failure", None), ("failure", FIRST), ("success", SECOND)]
)
def test_only_evidenced_success_is_returned(tmp_path, result, success):
    path = save(tmp_path, document(result, success))
    assert read_backup_status(path) == {
        "status": "available",
        "last_attempt": {"result": result, "timestamp": SECOND},
        "last_success_at": success,
    }


@pytest.mark.parametrize("result", ["success", "failure"])
def test_legacy_has_an_explicit_state_and_never_guesses_older_success(tmp_path, result):
    path = save(tmp_path, {"result": result, "timestamp": FIRST, "file": "/private/artifact"})
    assert read_backup_status(path) == {
        "status": "legacy",
        "last_attempt": {"result": result, "timestamp": FIRST},
        "last_success_at": FIRST if result == "success" else None,
    }


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        "secret",
        12,
        {},
        {"schema_version": 2},
        {"schema_version": True},
        {"result": "pending", "timestamp": FIRST},
        {"result": "success", "timestamp": "2026-10-24"},
        {"result": "failure", "timestamp": "2026-10-24T01:02:03"},
        document(success="private-key"),
        document(success="2026-10-26T00:00:00Z"),
        document(result="success", success=FIRST),
        document(result="success", success=None),
        {"schema_version": 1, "last_attempt": None, "last_success_at": None},
        {"schema_version": 1, "last_attempt": []},
        {"schema_version": 1, "last_attempt": {"result": "failure", "timestamp": FIRST}},
        {"result": [], "timestamp": FIRST},
        {"result": "failure", "timestamp": 123},
    ],
)
def test_invalid_evidence_is_not_partially_trusted(tmp_path, data):
    assert read_backup_status(save(tmp_path, data)) == {
        "status": "invalid",
        "last_attempt": None,
        "last_success_at": None,
    }


@pytest.mark.parametrize("raw", [b"{", b"\xff", b" " * 65_537, b"[" * 1200 + b"]" * 1200])
def test_broken_encoding_json_and_oversized_files(tmp_path, raw):
    path = tmp_path / "status.json"
    path.write_bytes(raw)
    assert read_backup_status(path)["status"] == "invalid"


def test_unconfigured_missing_directory_and_permission_errors_are_distinct(tmp_path, monkeypatch):
    assert read_backup_status(None)["status"] == "unconfigured"
    assert read_backup_status(tmp_path / "absent")["status"] == "missing"
    assert read_backup_status(tmp_path)["status"] == "unreadable"

    def denied(*args, **kwargs):
        raise PermissionError("/private/status secret")

    monkeypatch.setattr(Path, "open", denied)
    assert read_backup_status(tmp_path / "status.json") == {
        "status": "unreadable",
        "last_attempt": None,
        "last_success_at": None,
    }


def test_timestamps_are_converted_to_utc_before_ordering(tmp_path):
    path = save(
        tmp_path,
        document(
            timestamp="2026-10-25T03:02:03+02:00",
            success="2026-10-25T01:02:02.999999Z",
        ),
    )
    actual = read_backup_status(path)
    assert actual["status"] == "available"
    assert actual["last_attempt"]["timestamp"] == SECOND
    assert actual["last_success_at"] == "2026-10-25T01:02:02.999999Z"


def test_subsecond_success_cannot_be_later_than_attempt(tmp_path):
    path = save(tmp_path, document(success="2026-10-25T01:02:03.1Z"))
    assert read_backup_status(path)["status"] == "invalid"


@pytest.mark.parametrize("legacy_result", [None, "failure", "success"])
def test_writer_migrates_only_proven_legacy_success(tmp_path, legacy_result):
    legacy = tmp_path / "private.json"
    if legacy_result:
        legacy.write_text(json.dumps({"result": legacy_result, "timestamp": FIRST}))
    writer.record(tmp_path / "metadata", legacy, "failure", SECOND)
    report = read_backup_status(tmp_path / "metadata" / "status.json")
    assert report["last_success_at"] == (FIRST if legacy_result == "success" else None)
    assert report["last_attempt"]["result"] == "failure"


def test_writer_atomic_replacement_keeps_old_inode_and_publishes_only_metadata(tmp_path):
    directory = tmp_path / "metadata"
    legacy = tmp_path / "private.json"
    writer.record(directory, legacy, "success", FIRST)
    path = directory / "status.json"
    with path.open() as original:
        writer.record(directory, legacy, "failure", SECOND)
        assert json.load(original)["last_attempt"]["result"] == "success"
    assert read_backup_status(path)["last_success_at"] == FIRST
    assert json.loads(path.read_text()) == document()
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755
    assert {p.name for p in directory.iterdir()} == {"status.json", ".writer.lock"}


@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_write_failure_preserves_previous_evidence_and_cleans_temporary_file(
    tmp_path,
    monkeypatch,
    failure,
):
    directory = tmp_path / "metadata"
    legacy = tmp_path / "private.json"
    writer.record(directory, legacy, "success", FIRST)

    def fail(*args):
        raise OSError("/private/path secret")

    monkeypatch.setattr(writer.os, failure, fail)
    with pytest.raises(OSError):
        writer.record(directory, legacy, "failure", SECOND)
    assert read_backup_status(directory / "status.json")["last_attempt"]["result"] == "success"
    assert not list(directory.glob(".status-*"))


async def request(app, headers=None, path="/health/backup"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=BASE) as client:
        return await client.get(path, headers=headers)


async def test_success_failure_and_application_recreation_use_persisted_file(tmp_path, monkeypatch):
    directory = tmp_path / "metadata"
    legacy = tmp_path / "private.json"
    app = build(monkeypatch, backup_status_file=directory / "status.json")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    writer.record(directory, legacy, "success", FIRST)
    assert (await request(app, headers)).json()["last_success_at"] == FIRST
    writer.record(directory, legacy, "failure", SECOND)
    expected = {
        "status": "available",
        "last_attempt": {"result": "failure", "timestamp": SECOND},
        "last_success_at": FIRST,
    }
    assert (await request(app, headers)).json() == expected
    recreated = build(monkeypatch, backup_status_file=directory / "status.json")
    assert (await request(recreated, headers)).json() == expected


@pytest.mark.parametrize("kind", ["missing", "invalid", "unreadable", "legacy", "available"])
async def test_endpoint_redacts_paths_keys_and_raw_errors(tmp_path, monkeypatch, caplog, kind):
    private = "/private/operator/key-and-backup"
    path = tmp_path / "status.json"
    if kind == "invalid":
        path.write_text(private)
    elif kind == "unreadable":
        path.mkdir()
    elif kind == "legacy":
        save(
            tmp_path, {"result": "failure", "timestamp": FIRST, "message": private, "file": private}
        )
    elif kind == "available":
        save(tmp_path, {**document(), "message": private, "key": private, "file": private})
    app = build(monkeypatch, backup_status_file=path)
    response = await request(app, {"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.json()["status"] == kind
    assert set(response.json()) == {"status", "last_attempt", "last_success_at"}
    assert private not in response.text + caplog.text
    assert str(tmp_path) not in response.text + caplog.text
    anonymous = await request(app)
    assert anonymous.status_code == 401
    assert (await request(app, path="/health/live")).json()["status"] == "ok"


async def test_endpoint_works_when_auth_is_disabled_and_has_no_database_dependency(monkeypatch):
    app = build(monkeypatch, token=None)
    response = await request(app)
    assert response.status_code == 200
    assert response.json() == {
        "status": "unconfigured",
        "last_attempt": None,
        "last_success_at": None,
    }
