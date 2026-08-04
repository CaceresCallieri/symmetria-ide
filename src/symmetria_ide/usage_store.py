"""Cross-IDE subscription-usage peer channel — a shared tmpfs file, NOT the shell.

The multi-provider successor to `account_usage_store.py`. Same doctrine (a
single file in `/run/user/$UID`, last-write-wins, `QFileSystemWatcher` so every
open IDE window converges, survives a shell restart because the shell is not in
the path — see the prefer-peer-over-shell memory), with three differences that
the multi-provider shape forces:

1. **The merge is PER PROVIDER, not global.** Each entry carries its own
   `observed_at_ns`. Claude is observed often (every agent turn, via the
   status-line tap) while Codex is only polled every few minutes — a single
   global timestamp would let each fresh Claude observation clobber the Codex
   entry, and the panel would flicker Codex in and out forever.

2. **Two locks, deliberately.** `poll_lock` is leader election: with N IDE
   windows open, only the one that takes it performs the (network-bound, ~2s)
   poll, so the request count stays at one per interval no matter how many
   windows are up. `_write_lock` is a separate microsecond-scale mutex around
   the read-merge-write, so a status-line tap publishing on the GUI thread is
   never blocked behind an in-flight poll.

3. **A NEW file, leaving `symmetria-ide-account-usage.json` untouched.** The
   `main`/stable IDE — the user's daily driver — still writes that file through
   the old `publish_if_newer`, which serializes a fixed key list and would
   therefore DELETE this file's `providers` branch on every agent turn. The two
   channels stay separate until this ships to stable; the legacy one can be
   retired in the same promotion that removes its reader.

Module-level `read` / `publish_if_newer` / `poll_lock` are pure (no Qt) so they
unit-test directly; `UsageStore` is the thin `QObject` wrapper that owns the
watcher and emits `changed`.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, Signal

from .fs_atomic import atomic_write_json
from .usage_providers import ProviderUsage, usage_from_dict

__all__ = [
    "UsageStore",
    "default_usage_path",
    "poll_lock",
    "publish_if_newer",
    "read",
]

log = logging.getLogger(__name__)

_USAGE_FILENAME = "symmetria-ide-usage.json"


def _runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"


def default_usage_path() -> Path:
    return Path(_runtime_dir()) / _USAGE_FILENAME


def read(path: Path) -> dict[str, ProviderUsage]:
    """Load every provider snapshot, keyed by provider id. `{}` when absent.

    Tolerant by design: a missing file (nobody has published yet), a corrupt
    one, or an individual malformed entry all read as "no value" rather than
    raising — the panel simply shows nothing for that provider.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except OSError as exc:
        log.debug("usage-store read failed (%s)", exc)
        return {}
    providers = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(providers, dict):
        return {}
    out: dict[str, ProviderUsage] = {}
    for key, raw in providers.items():
        usage = usage_from_dict(raw)
        if usage is not None:
            out[str(key)] = usage
    return out


@contextmanager
def _write_lock(path: Path) -> Iterator[None]:
    """Blocking mutex around the read-merge-write. Held for microseconds.

    Without it, two windows publishing different providers at the same instant
    would each write back the OTHER's stale entry (classic read-modify-write
    race), and one update would be silently lost until its next observation.
    Blocking is safe precisely because the critical section excludes all I/O
    against the network — the long poll holds `poll_lock`, a different file.
    """
    lock_path = path.with_suffix(path.suffix + ".write.lock")
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        # A read-only or full runtime dir must not turn every agent turn into
        # an uncaught exception inside a queued GUI-thread slot. Degrade to an
        # unlocked write: `atomic_write_json` is still crash-safe, we only lose
        # the cross-process ordering guarantee — which is exactly the tradeoff
        # `poll_lock` already makes for the same failure.
        log.debug("usage-store: cannot open write lock (%s)", exc)
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the flock


@contextmanager
def poll_lock(path: Path) -> Iterator[bool]:
    """Leader election for the polling round. Yields True iff we hold it.

    NON-blocking on purpose: a window that loses the race must not queue up
    behind the winner and then poll again the moment it is released — it should
    simply skip this round and pick up the winner's result through the watcher.
    """
    lock_path = path.with_suffix(path.suffix + ".poll.lock")
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        log.debug("usage-store: cannot open poll lock (%s)", exc)
        yield False
        return
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            acquired = False  # another window is polling this round
        yield acquired
    finally:
        os.close(fd)


def publish_if_newer(path: Path, usage: ProviderUsage) -> bool:
    """Merge one provider's snapshot iff it beats the stored `observed_at_ns`.

    Returns True when it wrote. An errored snapshot (`observed_at_ns == 0` by
    the `usage_providers` error contract) can never win, so a failed fetch never
    blanks a good reading. An equal timestamp is also a no-op — that is what
    keeps our own write from looping our own watcher.
    """
    if usage.observed_at_ns <= 0:
        return False
    with _write_lock(path):
        current = read(path)
        existing = current.get(usage.provider)
        if existing is not None and existing.observed_at_ns >= usage.observed_at_ns:
            return False
        current[usage.provider] = usage
        payload = {"providers": {k: v.as_dict() for k, v in current.items()}}
        return atomic_write_json(path, payload)


class UsageStore(QObject):
    """Owns the shared-usage file watch; emits `changed` when anyone writes it.

    Thread model: lives on the GUI thread. `QFileSystemWatcher` delivers on the
    GUI thread, so consumers need no queued connection. `publish` / `read_current`
    are synchronous file I/O against tmpfs, acceptable inline (they run only on
    an actual freshness advance, not per frame).

    Mirrors `AccountUsageStore` including its watcher self-heal: an atomic write
    is a rename, and `QFileSystemWatcher` drops a path once it is renamed over —
    so the file must be re-added after every write, exactly as `GitController`
    does for its `.git` trigger files.
    """

    # Someone (a peer window, or us) wrote the file. No payload — the reader
    # pulls current state, which keeps this correct under coalesced signals.
    changed = Signal()

    def __init__(self, parent: QObject | None = None, *, path: Path | None = None):
        super().__init__(parent)
        self._path = path or default_usage_path()
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_file_changed)
        # The directory is watched too: the file may not exist yet at start (no
        # peer has published), and QFileSystemWatcher cannot watch a nonexistent
        # path. directoryChanged catches its first appearance.
        self._watcher.directoryChanged.connect(self._on_dir_changed)

    @property
    def path(self) -> Path:
        return self._path

    def start(self) -> None:
        """Begin watching. Idempotent."""
        parent = str(self._path.parent)
        if parent not in self._watcher.directories():
            self._watcher.addPath(parent)
        self._ensure_file_watched()

    def stop(self) -> None:
        paths = self._watcher.files() + self._watcher.directories()
        if paths:
            self._watcher.removePaths(paths)

    def read_current(self) -> dict[str, ProviderUsage]:
        return read(self._path)

    def publish(self, usage: ProviderUsage) -> bool:
        """Publish iff newer; on a successful write, re-arm the file watch.

        ⚠ GUI THREAD ONLY — it touches the QFileSystemWatcher. A worker thread
        must call the module-level `publish_if_newer` instead (see
        `UsagePoller._run_round`); the watcher re-arm it skips happens anyway
        when the rename fires `_on_file_changed` on the GUI thread.
        """
        wrote = publish_if_newer(self._path, usage)
        if wrote:
            self._ensure_file_watched()
        return wrote

    # -- watcher plumbing ------------------------------------------------

    def _ensure_file_watched(self) -> None:
        path = str(self._path)
        if os.path.exists(path) and path not in self._watcher.files():
            self._watcher.addPath(path)

    def _on_file_changed(self, _path: str) -> None:
        self._ensure_file_watched()
        self.changed.emit()

    def _on_dir_changed(self, _path: str) -> None:
        # The runtime dir churns constantly (sockets, other state files); react
        # only when OUR file's watch state actually changes, so a sibling's
        # churn is a no-op. A double `changed` after a rename (both signals can
        # fire, order unspecified) is harmless: the second read is identical.
        path = str(self._path)
        if os.path.exists(path) and path not in self._watcher.files():
            self._watcher.addPath(path)
            self.changed.emit()
