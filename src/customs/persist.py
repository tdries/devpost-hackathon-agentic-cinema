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
import time
from pathlib import Path

# The run's scratch tree is not worth mirroring -- extracted audio is large
# and nothing reads it once the run is over -- with one exception that is not
# scratch at all: the keyframes. Every observation points at one as its
# evidence, and the market room and the timeline draw it, so a restored run
# without them comes back with its findings intact and its proof missing.
_SKIP = {"work"}
_KEEP_INSIDE_SKIP = "frames"

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
        if item.is_dir() or item.suffix in (".db", ".db-wal", ".db-shm", ".tmp"):
            continue
        rel = item.relative_to(src)
        if rel.parts and rel.parts[0] in _SKIP and _KEEP_INSIDE_SKIP not in rel.parts:
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


def _snapshot_db(live: Path, dst: Path) -> bool:
    """Copy the live store out to the bucket.

    Two steps on purpose. sqlite's backup API makes the consistent copy, but
    it writes it to a local temporary file, not to the mount: opening a
    SQLite database *on* Cloud Storage FUSE fails outright (no locking, no
    atomic rename), which is exactly how the first version of this silently
    restored nothing. The finished file then goes across as plain bytes,
    which is the one thing FUSE does well.
    """
    if not live.is_file():
        return False
    tmp = live.with_suffix(".snapshot.tmp")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(str(live))
        target = sqlite3.connect(str(tmp))
        with target:
            source.backup(target)
        source.close()
        target.close()
        shutil.copy2(tmp, dst)
        return True
    except (sqlite3.Error, OSError):
        return False
    finally:
        tmp.unlink(missing_ok=True)


# The mount is shared, so the object under an open read handle can be
# replaced while we are copying it: during a rollout the outgoing revision
# snapshots while the incoming one restores. GCS FUSE surfaces that as
# ESTALE mid-copy ("stale file handle... modified or deleted by another
# process"). It happened on 2026-08-25 and cost a revision every one of its
# runs, silently, because the failure was swallowed and an empty store is
# indistinguishable from a first boot.
_RESTORE_TRIES = 4
_RESTORE_PAUSE_S = 1.5


def _usable(db: Path) -> bool:
    """Is this file actually a store, or the first half of one?

    A copy interrupted partway leaves a plausible-looking file that SQLite
    may open and report as empty, which is the worst possible outcome: the
    service comes up healthy and says there are no runs. Reading the table
    back is the only honest check.
    """
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            conn.execute("SELECT count(*) FROM runs").fetchone()
        return True
    except sqlite3.Error:
        return False


def _restore_db(src: Path, live: Path, on_note=None) -> bool:
    """Copy the bucket's store in. Plain bytes: the source is a finished
    snapshot, and SQLite must never be opened over the mount.

    Retried, because the copy can fail transiently, and verified, because it
    can also *succeed* into a truncated file. A restore that cannot be
    verified leaves nothing behind: a partial store on disk would look like
    a live one to restore()'s own "kept local store" check and never be
    retried on the next boot.
    """
    if not src.is_file():
        return False
    for attempt in range(1, _RESTORE_TRIES + 1):
        try:
            live.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, live)
            if _usable(live):
                return True
            reason = "copied file did not read back as a store"
        except OSError as exc:
            reason = f"{type(exc).__name__}: {exc}"
        live.unlink(missing_ok=True)
        if on_note is not None:
            on_note(f"restore attempt {attempt}/{_RESTORE_TRIES} failed: {reason}")
        if attempt < _RESTORE_TRIES:
            time.sleep(_RESTORE_PAUSE_S)
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
        notes: list[str] = []
        ok = _restore_db(state / local_db.name, local_db, on_note=notes.append)
        detail = f"restored store={ok} artifacts={files}"
        if notes:
            # Loud on purpose. This used to fail silently and the service
            # came up looking healthy with an empty board.
            detail += " | " + "; ".join(notes)
        return detail


def snapshot(db_path) -> str:
    """After anything that changed the store: push it to the bucket."""
    state = state_dir()
    if state is None:
        return "no state dir"
    local_db = Path(db_path)
    with _lock:
        ok = _snapshot_db(local_db, state / local_db.name)
        files = _mirror_files(local_db.parent, state)
    return f"snapshot store={ok} artifacts={files}"
