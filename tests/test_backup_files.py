"""Encrypted artifacts are published completely and never replace an existing name."""

import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services import backup_files


@pytest.mark.parametrize("method", ["age", "gpg"])
def test_private_allocation_and_concurrent_no_clobber_publication(tmp_path, method):
    target = tmp_path / f"health-fixed.dump.{method}"
    target.write_bytes(b"previous-success")
    partials = [backup_files.create_partial(target) for _ in range(2)]
    assert len(set(partials)) == 2
    for i, partial in enumerate(partials):
        assert stat.S_IMODE(partial.stat().st_mode) == 0o600
        partial.write_bytes(f"complete-ciphertext-{i}".encode())
    with target.open("rb") as original, ThreadPoolExecutor(2) as pool:
        outputs = list(pool.map(lambda p: backup_files.publish(p, target), partials))
        assert original.read() == b"previous-success"
    assert target.read_bytes() == b"previous-success"
    assert len(set(outputs)) == 2
    assert [p.read_bytes() for p in outputs] == [b"complete-ciphertext-0", b"complete-ciphertext-1"]
    assert all(p in tmp_path.glob(f"health-*.dump.{method}") for p in outputs)
    assert not any(p.exists() for p in partials)


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_occupied_names_including_dangling_links_are_preserved(tmp_path, kind):
    target = tmp_path / "health-fixed.dump.age"
    if kind == "file":
        target.write_bytes(b"old")
    elif kind == "directory":
        target.mkdir()
    else:
        target.symlink_to(tmp_path / "missing")
    before = target.lstat()
    partial = backup_files.create_partial(target)
    partial.write_bytes(b"ciphertext")
    output = backup_files.publish(partial, target)
    assert output != target and output.read_bytes() == b"ciphertext"
    assert target.lstat() == before


@pytest.mark.parametrize("failure", ["empty", "link", "collisions"])
def test_failure_keeps_existing_artifact_and_only_callers_partial(tmp_path, monkeypatch, failure):
    target = tmp_path / "health-fixed.dump.age"
    target.write_bytes(b"old")
    partial = backup_files.create_partial(target)
    peer = backup_files.create_partial(target)
    if failure != "empty":
        partial.write_bytes(b"ciphertext")

        def refused(*args):
            raise FileExistsError if failure == "collisions" else OSError("link unavailable")

        monkeypatch.setattr(backup_files.os, "link", refused)
    with pytest.raises((ValueError, OSError)):
        backup_files.publish(partial, target)
    assert target.read_bytes() == b"old"
    assert partial.exists() and peer.exists()
    assert set(tmp_path.iterdir()) == {target, partial, peer}
