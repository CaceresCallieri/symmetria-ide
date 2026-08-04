"""Cross-IDE account-usage peer channel — a shared tmpfs file, NOT the shell.

Claude account rate-limits (5h / 7d usage) are GLOBAL to one login, so every
IDE instance observing them through its agents' status-line taps should converge
on the single freshest observation — including idle instances on other
workspaces that haven't transacted in hours (the staleness this whole feature
fixes).

The channel is deliberately a **peer** mechanism, not routed through Symmetria
Shell's `agent-bridge.py` hub: a core IDE flow must survive a different shell or
a shell restart, and account usage has nothing to do with the shell. (This
matches the project's agent-ownership inversion — agent capture already moved
OFF the bridge onto the IDE's own socket.) A single file beats an IDE-to-IDE
socket mesh: no discovery, no connection management, no N×N fanout, and the last
value PERSISTS even when every IDE is closed (a freshly-launched IDE reads it at
start and shows usage immediately, before any of its own agents transact).

Coordination is **last-write-wins by `observed_at_ns`**: `publish_if_newer`
writes only when its observation beats what's on disk, so writes stay infrequent
(rate limits change rarely). `atomic_write_json`'s rename means a reader only
ever sees a complete JSON; a rare simultaneous-write race just lets a marginally
older value win briefly, self-corrected by the next observation. (A strict
compare-and-swap via `flock` is unnecessary for advisory display data — add it
only if it ever actually bites.)

Module-level `read`/`publish_if_newer` are pure (no Qt) so they unit-test
directly; `AccountUsageStore` is the thin `QObject` wrapper that owns a
`QFileSystemWatcher` and emits `changed` — the `GitController` watcher pattern.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, Signal

from .fs_atomic import atomic_write_json

__all__ = ["AccountUsageStore", "default_usage_path", "read", "publish_if_newer"]

log = logging.getLogger(__name__)

# Single per-user file (the account is global to one Claude login). Lives in
# tmpfs alongside the agent sockets — same convention as agent_events.
_USAGE_FILENAME = "symmetria-ide-account-usage.json"

# The fields one observation carries. `observed_at_ns` (CLOCK_REALTIME, set by
# the tap) is the merge key; the rest is what StatusBar renders.
_FIELDS = (
    "five_pct",
    "five_reset",
    "seven_pct",
    "seven_reset",
    "observed_at_ns",
)


def _runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"


def default_usage_path() -> Path:
    return Path(_runtime_dir()) / _USAGE_FILENAME


def read(path: Path) -> dict | None:
    """Load the shared observation, or None if absent/partial/corrupt.

    Tolerant by design: a missing file (no peer has published yet) or a
    transiently half-written one (read racing a non-atomic writer — shouldn't
    happen given atomic_write_json, but defensive) both read as "no value".
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    except OSError as exc:
        log.debug("account-usage read failed (%s)", exc)
        return None
    if not isinstance(data, dict) or "observed_at_ns" not in data:
        return None
    return data


def publish_if_newer(path: Path, usage: dict) -> bool:
    """Write `usage` to `path` iff it beats the on-disk `observed_at_ns`.

    Returns True if it wrote (this observation is now the global freshest),
    False if a newer/equal value already sits on disk (no-op — including our
    OWN last write, so this never loops the watcher). `usage` must carry
    `observed_at_ns`; absent → treated as 0 (never wins).
    """
    incoming = int(usage.get("observed_at_ns", 0) or 0)
    if incoming <= 0:
        return False
    current = read(path)
    if current is not None and int(current.get("observed_at_ns", 0) or 0) >= incoming:
        return False
    return atomic_write_json(path, {key: usage.get(key) for key in _FIELDS})


class AccountUsageStore(QObject):
    """Owns the shared-usage file watch; emits `changed` when a peer writes it.

    Thread model: lives on the GUI thread. `QFileSystemWatcher` delivers
    `fileChanged` / `directoryChanged` on the GUI thread, so consumers need no
    queued connection. `publish` / `read_current` are plain synchronous file I/O
    (infrequent — only on a freshest-usage advance), acceptable inline.
    """

    # A peer (or this IDE) wrote the file. AppController re-reads and adopts the
    # value when it beats its own. No payload — the reader pulls current state.
    changed = Signal()

    def __init__(self, parent: QObject | None = None, *, path: Path | None = None):
        super().__init__(parent)
        self._path = path or default_usage_path()
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_file_changed)
        # Watch the directory too: the file may not exist yet at start (a peer
        # creates it later), and QFileSystemWatcher cannot watch a nonexistent
        # path. directoryChanged catches its first appearance.
        self._watcher.directoryChanged.connect(self._on_dir_changed)

    def start(self) -> None:
        """Begin watching. Idempotent. Adds the file if it exists, plus its dir."""
        parent = str(self._path.parent)
        if parent not in self._watcher.directories():
            self._watcher.addPath(parent)
        self._ensure_file_watched()

    def stop(self) -> None:
        paths = self._watcher.files() + self._watcher.directories()
        if paths:
            self._watcher.removePaths(paths)

    def read_current(self) -> dict | None:
        return read(self._path)

    def publish(self, usage: dict) -> None:
        """Publish iff newer; on a successful write, re-arm the file watch.

        Only a write that wins (newer ts) renames the file, and only a rename
        drops the watch — so the re-arm is correctly conditional on the write.
        """
        if publish_if_newer(self._path, usage):
            self._ensure_file_watched()

    # -- watcher plumbing ------------------------------------------------

    def _ensure_file_watched(self) -> None:
        """(Re)add the file path when it exists and isn't already watched.

        QFileSystemWatcher stops watching a path after the file is replaced via
        rename (every atomic write) — the same self-heal GitController does for
        its `.git` trigger files.
        """
        path = str(self._path)
        if os.path.exists(path) and path not in self._watcher.files():
            self._watcher.addPath(path)

    def _on_file_changed(self, _path: str) -> None:
        self._ensure_file_watched()
        self.changed.emit()

    def _on_dir_changed(self, _path: str) -> None:
        # The runtime dir churns (sockets, etc.); only react when OUR file's
        # watch state actually changes — i.e. it just appeared and isn't yet
        # watched. This guard makes a sibling's churn a no-op.
        #
        # After an atomic rename, BOTH fileChanged and directoryChanged can fire
        # and Qt does not guarantee their order. If directoryChanged lands first
        # (the file was momentarily unwatched by the rename), this re-adds + emits,
        # then fileChanged emits again — a double `changed`. That is harmless by
        # design: the second _on_shared_usage_changed read is an equal-ts no-op in
        # _adopt_account_usage. So "emit once" is the common case, not a guarantee.
        path = str(self._path)
        if os.path.exists(path) and path not in self._watcher.files():
            self._watcher.addPath(path)
            self.changed.emit()
