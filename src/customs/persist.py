"""Keep runs across deploys.

Cloud Run gives a container a fresh filesystem on every revision, and the run
store is SQLite in that filesystem, so a deploy used to erase every run: the
board, the findings, the evidence frames and the localized masters all went
with it. This module mirrors the run directory to a Cloud Storage bucket that
the service mounts as a volume (CUSTOMS_STATE_DIR), and restores from it on
boot.

Why a mirror and not simply putting SQLite on the mounted bucket: Cloud
Storage FUSE has no POSIX file locking and no atomic rename, which is exactly
what SQLite relies on, and Google says as much. So the database keeps living
on the container's own disk where its locking works, and what lands on the
bucket is a *consistent copy* made with sqlite3's own backup API (stdlib, and
safe to run while the database is open and being written).

Artifacts (uploads, frames, localized masters, change stills) are ordinary
files, which FUSE handles fine, so those are copied straight across, newest
wins, and only when they are missing or a different size.

ponytail: copy-in, copy-out, one lock. Not a replication protocol. It holds
because exactly one instance ever writes (--max-instances 1); the day that
stops being true, this needs a real database instead of a bigger version of
this file.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from pathlib import Path

# Scratch frames and audio: regenerable, large, and never read after a run
# finishes. Mirroring them would triple the copy for nothing.
_SKIP = {"work"}

_lock = threading.Lock()


def state_dir() -> Path | None:
    """The mounted bucket, or None when this instance runs without one."""
    raw = os.environ.get("CUSTOMS_STATE_DIR", "").strip()
    if not raw:
        return None
    path = Path(raw)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return path if path.is_dir() else None


def _mirror_files(src: Path, dst: Path) -> int:
    """Copy every file under src that dst lacks (or has at a different size)."""
    copied = 0
    for item in src.rglob("*"):
        if item.is_dir() or item.suffix in (".db", ".db-wal", ".db-shm"):
            continue
        rel = item.relative_to(src)
        if rel.parts and rel.parts[0] in _SKIP:
            continue
        target = dst / item.relative_to(src)
        try:
            if target.exists() and target.stat().st_size == item.stat().st_size:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            copied += 1
        except OSError:
            continue
    return copied


def _copy_db(src: Path, dst: Path) -> bool:
    """A consistent snapshot of the store, taken with sqlite's backup API."""
    if not src.is_file():
        return False
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        target = sqlite3.connect(str(dst))
        with target:
            source.backup(target)
        source.close()
        target.close()
        return True
    except sqlite3.Error:
        return False


def restore(db_path) -> str:
    """Boot: bring the previous revision's runs back into this container.

    The database is only restored when this container has none of its own, so
    a live store is never overwritten by an older snapshot. Artifacts are
    merged either way, because a file that exists in the bucket and not here
    is always something this container is missing.
    """
    state = state_dir()
    if state is None:
        return "no state dir"
    local_db = Path(db_path)
    runs = local_db.parent
    runs.mkdir(parents=True, exist_ok=True)
    with _lock:
        files = _mirror_files(state, runs)
        if local_db.exists() and local_db.stat().st_size > 0:
            return f"kept local store, merged {files} artifact(s)"
        ok = _copy_db(state / local_db.name, local_db)
        return f"restored store={ok} artifacts={files}"


def snapshot(db_path) -> str:
    """After anything that changed the store: push it to the bucket."""
    state = state_dir()
    if state is None:
        return "no state dir"
    local_db = Path(db_path)
    with _lock:
        ok = _copy_db(local_db, state / local_db.name)
        files = _mirror_files(local_db.parent, state)
    return f"snapshot store={ok} artifacts={files}"
