"""Recursive working-tree watcher that wakes ``GitController`` re-scans.

Why this exists: ``GitController``'s ``QFileSystemWatcher`` covers
``.git/{index,HEAD,MERGE_HEAD,refs/heads/<branch>}`` — git's OWN state
transitions (stage / commit / branch switch). Ordinary working-tree
activity (saving a file in nvim, an agent rewriting sources, ``echo >>
file`` in the shell pane, creating an untracked file) never touches
``.git``, so the Active Changes panel and the tree badges stayed frozen
until the next git operation. This watcher closes that gap.

Why watchdog and not more ``QFileSystemWatcher`` entries:
``QFileSystemWatcher`` is non-recursive (every directory must be added
individually and newly created directories re-armed by hand) and its
directory watches report only entry create/delete/rename — NOT in-place
file modification, which is exactly the event an editor save or an agent
``Edit`` produces. watchdog's inotify backend is recursive, follows new
subdirectories automatically, and reports modifications.

Threading model:
  - watchdog's ``Observer`` runs its own threads (watchdog's
    ``BaseThread`` sets ``daemon=True``); ``stop()`` joins them, so both
    halves of project-standards §1 P0 are satisfied.
  - The event callback runs on the observer's emitter thread. It must
    not touch Qt GUI objects, so it emits the ``changed`` signal through
    ``emit_gc_safe`` (gotcha #10 — the queued-connection marshalling
    allocates) and the receiver connects with an explicit
    ``Qt.QueuedConnection`` (§4 P2; connect site in ``GitController``).
  - ``set_root`` / ``stop`` are GUI-thread-only, mirroring the
    ``QFileSystemWatcher`` lifecycle they sit alongside.

Failure mode: scheduling a recursive watch can exhaust the kernel's
inotify watch budget (``fs.inotify.max_user_watches``) on pathological
trees. That raises ``OSError`` from ``Observer.schedule``/``start`` —
we log a warning and degrade to the pre-existing behavior (git-state
changes still refresh via the ``.git`` watcher; working-tree edits
refresh on the next editor-save poke or git operation) rather than
crash.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal

from .agent_bridge import emit_gc_safe

log = logging.getLogger(__name__)

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only on hosts without watchdog
    WATCHDOG_AVAILABLE = False
    FileSystemEventHandler = object  # type: ignore[assignment, misc]
    Observer = None  # type: ignore[assignment, misc]

# Emitter-thread throttle. A burst (agent refactor touching 50 files,
# `rm -rf` of a directory) produces hundreds of events within
# milliseconds; each surviving event queues one cross-thread emit. One
# emit per 50ms window is plenty — the GUI-side 200ms debounce coalesces
# anyway, this just keeps the Qt event queue from flooding mid-burst.
_EMIT_THROTTLE_SEC = 0.05

# Event types that can change `git status` output. watchdog's inotify
# backend also reports "opened" / "closed_no_write" (a plain `grep -r`
# or nvim opening a buffer fires these) — re-scanning on reads would
# make every navigation action fork git, so they are filtered out.
# "closed" (closed-after-write) is redundant with "modified".
_RELEVANT_EVENT_TYPES = frozenset({"created", "deleted", "modified", "moved"})


def _is_git_internal(path: str) -> bool:
    """True for paths inside a ``.git`` directory (or ``.git`` itself).

    Working-tree watching must not react to git's internal churn — every
    scan WE run touches ``.git`` (index mtime refresh), so without this
    filter each scan would schedule the next one, forever. The four
    meaningful ``.git`` trigger files have their own dedicated watcher in
    ``GitController``.
    """
    return path.endswith("/.git") or "/.git/" in path


class _RescanEventHandler(FileSystemEventHandler):  # type: ignore[misc]
    """Thin trampoline: watchdog callback → owning watcher's filter."""

    def __init__(self, watcher: WorktreeWatcher) -> None:
        super().__init__()
        self._watcher = watcher

    def on_any_event(self, event: FileSystemEvent) -> None:  # pragma: no cover - thin
        self._watcher.handle_fs_event(event)


class WorktreeWatcher(QObject):
    """Watches a repo's working tree recursively and signals on changes.

    Owned by ``GitController``; rebuilt via ``set_root`` whenever the
    resolved repo root changes (same lifecycle moment as the ``.git``
    trigger-file watcher). ``is_ignored`` is the controller's
    thread-safe gitignore membership test — events under ignored paths
    (``node_modules`` churn, build outputs) cannot change ``git status``
    output and are dropped on the emitter thread before any Qt traffic.
    """

    # No payload — the receiver only needs "something changed, debounce a
    # scan". Connected with explicit Qt.QueuedConnection in GitController.
    changed = Signal()

    def __init__(
        self,
        is_ignored: Callable[[str], bool],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._is_ignored = is_ignored
        self._root = ""
        self._observer: Observer | None = None
        self._last_emit_monotonic = 0.0

    def set_root(self, root: str) -> None:
        """(Re)point the recursive watch. Idempotent on equal roots.

        Empty string tears the watch down without starting a new one
        (not-a-repo / teardown case). GUI thread only.
        """
        if root == self._root:
            return
        self._root = root
        self._stop_observer()
        if not root or not WATCHDOG_AVAILABLE:
            if root and not WATCHDOG_AVAILABLE:
                log.warning(
                    "watchdog not installed — working-tree changes will not "
                    "auto-refresh git status (install python-watchdog)"
                )
            return
        observer = Observer()
        try:
            observer.schedule(_RescanEventHandler(self), root, recursive=True)
            observer.start()
        except OSError as exc:
            # Most likely inotify watch exhaustion on a huge tree. Degrade
            # to .git-watcher-only refresh rather than crash the IDE.
            log.warning(
                "working-tree watch failed for %s (%s) — git status will "
                "only refresh on git operations and editor saves",
                root,
                exc,
            )
            self._teardown(observer)
            return
        self._observer = observer
        log.debug("working-tree watch armed: %s", root)

    def stop(self) -> None:
        """Tear the observer down and join its threads. GUI thread only."""
        self._root = ""
        self._stop_observer()

    # -- Emitter-thread path -------------------------------------------------

    def handle_fs_event(self, event: FileSystemEvent) -> None:
        """Filter one watchdog event; emit ``changed`` if it can affect status.

        Runs on the observer's emitter thread — Qt access limited to the
        gc-guarded signal emit.
        """
        if event.event_type not in _RELEVANT_EVENT_TYPES:
            return
        if not self._any_path_relevant(event):
            return
        now = time.monotonic()
        if now - self._last_emit_monotonic < _EMIT_THROTTLE_SEC:
            return
        self._last_emit_monotonic = now
        emit_gc_safe(self.changed)

    def _any_path_relevant(self, event: FileSystemEvent) -> bool:
        """True when src or move-dest is neither git-internal nor ignored."""
        paths = [str(event.src_path)]
        dest = getattr(event, "dest_path", "")
        if dest:
            paths.append(str(dest))
        return any(not _is_git_internal(p) and not self._is_ignored(p) for p in paths)

    # -- Internals -------------------------------------------------------------

    def _stop_observer(self) -> None:
        if self._observer is None:
            return
        observer, self._observer = self._observer, None
        self._teardown(observer)

    @staticmethod
    def _teardown(observer: Observer) -> None:
        try:
            observer.stop()
            observer.join(timeout=1.0)
        except Exception:  # noqa: BLE001 - teardown must never propagate
            log.exception("working-tree watcher teardown failed")
