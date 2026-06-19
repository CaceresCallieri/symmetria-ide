"""Atomic JSON write — the one crash-safe write path the IDE's on-disk
state files share.

A naive ``open(path, "w") + json.dump`` leaves a truncated/half-written
file if the process dies mid-write (or the disk fills). The standard fix
is write-to-temp-then-rename: ``os.replace`` is atomic on POSIX when both
paths sit on the same filesystem, so a reader ever only sees the old file
or the complete new one — never a partial.

Extracted from ``tree_state_cache.save_expanded`` (the original home of
this dance) so the per-project marker writer (``project_browser_marker``)
reuses the exact same semantics instead of copy-pasting it. Any future
on-disk state file should call this rather than rolling its own.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def atomic_write_json(target: Path, payload: dict) -> bool:
    """Atomically serialize ``payload`` to ``target`` as pretty JSON.

    Creates ``target``'s parent directory if absent. Writes a temp file in
    the SAME directory as ``target`` (``dir=`` is load-bearing: ``os.replace``
    is atomic only within one filesystem, and the default tmp dir may live
    on another), fsyncs it, then renames over ``target``. A crash between
    the write and the rename leaves the original ``target`` untouched.

    Returns ``True`` on success, ``False`` on any ``OSError`` (logged) — the
    caller decides whether a failed state write is fatal (for caches and
    per-project settings it never is). Mirrors the fault-tolerance of the
    matching loaders, which treat a missing/corrupt file as "no state".
    """
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    tmp_fd: int | None = None
    tmp_path: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=f".{target.name}-",
            suffix=".tmp",
            dir=str(target.parent),
        )
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            tmp_fd = None  # ownership passed to the file object
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
        tmp_path = None
        return True
    except OSError as e:
        log.warning("atomic_write_json: write failed for %s: %s", target, e)
        return False
    finally:
        # Defensive cleanup if the open or replace raised before we
        # relinquished ownership. Errors here are silent — a leaked tmp
        # file in the destination dir is harmless.
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
