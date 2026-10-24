"""Writes made by the admin panel: atomic, guarded against lost updates, undoable.

The files under the config directory stay the source of truth -- every request
already re-reads them -- so a save from the panel is just a careful file write:

  * **atomic**: the new content goes to a sibling temp file, is fsync'ed, and
    replaces the old one with `os.replace`. A reader sees the old file or the
    new one, never half of each.
  * **no lost updates**: the editor sends back the ETag of what it loaded; if
    the file changed since (someone else saved, or IT edited it by hand), the
    write is refused with `Conflict`.
  * **undoable**: the content being replaced is kept in `<config_dir>/.history`,
    the last `KEEP` versions per document, so any save can be rolled back.

Snapshots of users.json hold password hashes, so history files are 0600 in a
0700 directory, like the file they come from should be.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

HISTORY_DIR = ".history"
#: Versions kept per document. Older ones are pruned on every save.
KEEP = 30
_SNAPSHOT_ID = re.compile(r"^\d{8}T\d{6}\d{6}Z$")


class StoreError(RuntimeError):
    """A write cannot be carried out."""


class Conflict(StoreError):
    """The file changed since the editor loaded it."""


def etag(data: bytes | None) -> str:
    """Identifies one exact content; "absent" for a file that does not exist."""
    return "absent" if data is None else hashlib.sha256(data).hexdigest()[:32]


def read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def writable(path: Path) -> bool:
    """Whether a file at `path` could be replaced (or created) by this process."""
    directory = path.parent
    while not directory.exists():
        if directory.parent == directory:
            return False
        directory = directory.parent
    return os.access(directory, os.W_OK | os.X_OK)


@dataclass(frozen=True)
class Snapshot:
    id: str
    time: float
    size: int


class Store:
    def __init__(self, config_dir: Path) -> None:
        self.root = config_dir / HISTORY_DIR
        # Re-entrant: `modify` holds it while calling `write`.
        self._lock = threading.RLock()

    # -- writes ---------------------------------------------------------------

    def write(self, key: str, path: Path, data: bytes, *, expected: str | None = None, mode: int = 0o644) -> str:
        """Replace `path` with `data`; returns the new ETag.

        `expected` is the ETag the caller last saw. None skips the check, for
        changes that are computed from the current content under the lock.
        """
        with self._lock:
            current = read(path)
            if expected is not None and expected != etag(current):
                raise Conflict(f"{path.name} changed since it was loaded")
            if current == data:
                return etag(data)
            if current is not None:
                self._snapshot(key, current)
            mode = path.stat().st_mode & 0o777 if current is not None else mode
            self._replace(path, data, mode)
            return etag(data)

    def modify(self, key: str, path: Path, change: Callable[[bytes | None], bytes], *, mode: int = 0o644) -> str:
        """Read, transform and write back as one step, so concurrent edits serialise."""
        with self._lock:
            return self.write(key, path, change(read(path)), mode=mode)

    def delete(self, key: str, path: Path, *, expected: str | None = None) -> None:
        with self._lock:
            current = read(path)
            if current is None:
                raise FileNotFoundError(path)
            if expected is not None and expected != etag(current):
                raise Conflict(f"{path.name} changed since it was loaded")
            self._snapshot(key, current)
            path.unlink()

    @staticmethod
    def _replace(path: Path, data: bytes, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    # -- history --------------------------------------------------------------

    def _directory(self, key: str) -> Path:
        # Readable and collision free: "skill:a/b.md" and "skill:a_b.md" slug
        # alike, the hash keeps them apart.
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", key).strip("._")[:80] or "document"
        return self.root / f"{slug}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]}"

    def _snapshot(self, key: str, data: bytes) -> None:
        directory = self._directory(key)
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        os.chmod(directory, 0o700)
        now = time.time_ns()
        snapshot = directory / (time.strftime("%Y%m%dT%H%M%S", time.gmtime(now // 1_000_000_000)) + f"{now // 1000 % 1_000_000:06d}Z")
        self._replace(snapshot, data, 0o600)
        for stale in sorted(directory.iterdir(), reverse=True)[KEEP:]:
            stale.unlink(missing_ok=True)

    def history(self, key: str) -> list[Snapshot]:
        """Previous versions of a document, newest first."""
        directory = self._directory(key)
        if not directory.is_dir():
            return []
        snapshots: list[Snapshot] = []
        for item in sorted(directory.iterdir(), reverse=True):
            if not _SNAPSHOT_ID.match(item.name):
                continue
            stat = item.stat()
            snapshots.append(Snapshot(id=item.name, time=stat.st_mtime, size=stat.st_size))
        return snapshots

    def snapshot(self, key: str, snapshot_id: str) -> bytes:
        if not _SNAPSHOT_ID.match(snapshot_id):
            raise FileNotFoundError(snapshot_id)
        return (self._directory(key) / snapshot_id).read_bytes()
