"""Host-side, stdlib-only allocation and no-clobber publication of encrypted dumps."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4


def create_partial(target: Path) -> Path:
    """Exclusively reserve a private file beside the intended final artifact."""
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".partial", dir=target.parent)
    os.close(fd)
    return Path(name)


def publish(partial: Path, target: Path) -> Path:
    """Link complete ciphertext atomically; an existing name is never replaced.

    Both paths must be on the same filesystem. Unsupported hard links or exhausted
    name retries fail closed, leaving the caller's partial available for cleanup.
    Keep the .dump.age/.dump.gpg suffix for discovery, retention and restore.
    """
    stem, separator, method = target.name.rpartition(".dump.")
    if not stem or not separator or method not in ("age", "gpg"):
        raise ValueError("encrypted dump target required")
    if partial.parent.resolve() != target.parent.resolve() or partial == target:
        raise ValueError("a separate temporary file in the backup directory is required")
    with partial.open("rb") as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            raise ValueError("encrypted dump is empty")
        os.fsync(stream.fileno())

    candidate = target
    for _ in range(10):
        try:
            # Unlike rename/replace, link is an atomic create-if-absent operation,
            # including when candidate is a dangling symlink or a directory.
            os.link(partial, candidate)
        except FileExistsError:
            candidate = target.with_name(f"{stem}-{uuid4().hex}.dump.{method}")
        else:
            directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
                partial.unlink()
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return candidate
    raise FileExistsError("could not allocate an unused backup name")


def main(args: list[str]) -> int:
    """Internal shell adapter: stdout carries only a successfully allocated path."""
    try:
        if len(args) == 2 and args[0] == "create":
            path = create_partial(Path(args[1]))
        elif len(args) == 3 and args[0] == "publish":
            path = publish(Path(args[1]), Path(args[2]))
        else:
            raise ValueError("invalid operation or arguments")
        print(path)
    except (OSError, ValueError):
        print("backup: could not allocate or publish encrypted dump", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
