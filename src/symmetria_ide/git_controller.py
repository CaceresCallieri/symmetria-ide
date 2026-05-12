"""Git status parsing + the GitController Qt facade.

Three layers, top-down:

  1. ``parse_porcelain_v2(blob: bytes) -> dict[str, GitStatus]`` — a pure
     parser for `git status --porcelain=v2 -z` stdout. No subprocess, no Qt,
     no I/O. Feed it bytes, get a path-keyed status map.

  2. ``_add_directory_aggregates(file_map) -> dict`` — a pure post-processor
     that synthesises aggregate entries for every ancestor directory of
     every changed path, so the file tree can render a badge on directories
     whose subtrees contain changes.

  3. ``GitController(QObject)`` — the Qt facade. Owns a worker thread that
     runs ``git rev-parse`` + ``git status`` on demand, a ``QFileSystemWatcher``
     on ``.git/{index,HEAD,MERGE_HEAD,refs/heads/<branch>}`` to trigger
     re-scans, and a 200ms debounce timer to coalesce bursts. Exposes
     ``statusForPath(absolute_path) -> QVariantMap`` to QML and emits
     ``statusChanged`` once per scan.

Future phases will grow ``GitController`` with ``stage()`` / ``unstage()`` /
``commit()`` / ``branchList()`` slots — per the "same class, additive"
decision, the same object that owns the status map will own the operations
surface. This keeps the watcher and parsed map as a single source of truth.
"""

from __future__ import annotations

import gc
import logging
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QFileSystemWatcher,
    QModelIndex,
    QObject,
    Qt,
    QTimer,
    Signal,
    Slot,
)

log = logging.getLogger(__name__)


# Semantic state names — map to FmTheme.gitStatus.* colors in QML.
# These are stable identifiers; the actual hex values live FM-side.
STATE_UNSTAGED = "unstaged"  # red    — worktree changes not yet staged
STATE_STAGED = "staged"  # green  — staged for next commit
STATE_UNTRACKED = "untracked"  # blue   — new file, never added to git
STATE_RENAMED = "renamed"  # yellow — staged rename or copy
STATE_CONFLICTED = "conflicted"  # magenta — unmerged
STATE_IGNORED = "ignored"  # gray   — listed in .gitignore (rarely rendered)


@dataclass(slots=True, frozen=True)
class GitStatus:
    """One file's git status, normalized for badge rendering.

    `path` is repo-relative (porcelain v2 always emits paths relative to
    the repo root). `char` is the single character the badge displays.
    `state` selects the color via the FM's named-color palette.
    `orig_path` is populated only for rename/copy records (type `2`).
    """

    path: str
    char: str
    state: str
    tooltip: str
    orig_path: str | None = None


# Human-readable tooltips, indexed by (char, state).
# Unlisted combinations fall back to a generic "<state> (<char>)" at parse time
# so we never crash on a status code we haven't catalogued yet.
_TOOLTIPS: dict[tuple[str, str], str] = {
    ("M", STATE_UNSTAGED): "Modified",
    ("M", STATE_STAGED): "Modified (staged)",
    ("A", STATE_STAGED): "Added (staged)",
    ("D", STATE_UNSTAGED): "Deleted",
    ("D", STATE_STAGED): "Deleted (staged)",
    ("R", STATE_RENAMED): "Renamed",
    ("C", STATE_RENAMED): "Copied",
    ("T", STATE_UNSTAGED): "Type changed",
    ("T", STATE_STAGED): "Type changed (staged)",
    ("?", STATE_UNTRACKED): "Untracked",
    ("!", STATE_IGNORED): "Ignored",
    ("U", STATE_CONFLICTED): "Conflicted",
}


def _classify_xy(x: str, y: str) -> tuple[str, str]:
    """Reduce porcelain XY to a single (char, state).

    Worktree status (Y) takes precedence over index status (X) — the LazyGit
    convention. So an "MM" file (staged-then-modified) renders as unstaged
    red, not staged green: the user's most recent action is what they need
    to see first. The dual-badge "MM" rendering is a future seam, not v1.
    """
    if y != ".":
        return (y, STATE_UNSTAGED)
    if x != ".":
        return (x, STATE_STAGED)
    # Both dots means "no change" — porcelain doesn't emit these, but if a
    # future git version did, returning a sentinel makes the bug visible
    # rather than silently producing a misleading badge.
    return (".", STATE_UNSTAGED)


def _tooltip_for(char: str, state: str) -> str:
    return _TOOLTIPS.get((char, state), f"{state} ({char})")


def parse_porcelain_v2(blob: bytes) -> dict[str, GitStatus]:
    """Parse `git status --porcelain=v2 -z` stdout into a path→GitStatus map.

    Record formats (NUL-terminated when `-z` is in effect):

      ``# branch.<field> <value>``                                — header (skipped)
      ``1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>``            — ordinary
      ``2 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <X><score> <new>``  — rename/copy
        ↑ followed by a second NUL-terminated field carrying the original path.
      ``u <XY> <sub> <m1> <m2> <m3> <mW> <h1> <h2> <h3> <path>``  — unmerged
      ``? <path>``                                                — untracked
      ``! <path>``                                                — ignored

    Malformed records are silently skipped — partial output from a torn
    `git status` (process killed mid-write) must not crash the watcher loop.
    """
    result: dict[str, GitStatus] = {}
    if not blob:
        return result

    # NUL-split. Drop the trailing empty that always follows the final NUL.
    fields = blob.split(b"\x00")
    if fields and fields[-1] == b"":
        fields.pop()

    i = 0
    while i < len(fields):
        rec = fields[i].decode("utf-8", errors="replace")
        i += 1

        if not rec or rec.startswith("#"):
            continue

        if rec.startswith("1 "):
            parts = rec.split(" ", 8)
            if len(parts) < 9 or len(parts[1]) < 2:
                continue
            xy = parts[1]
            path = parts[8]
            char, state = _classify_xy(xy[0], xy[1])
            result[path] = GitStatus(
                path=path,
                char=char,
                state=state,
                tooltip=_tooltip_for(char, state),
            )

        elif rec.startswith("2 "):
            parts = rec.split(" ", 9)
            if len(parts) < 10 or len(parts[1]) < 2 or i >= len(fields):
                # Rename without its companion origPath field — malformed; skip
                # AND consume nothing further so the next iteration realigns.
                continue
            xy = parts[1]
            path = parts[9]
            orig_path = fields[i].decode("utf-8", errors="replace")
            i += 1
            char, state = _classify_xy(xy[0], xy[1])
            # Rename/copy use the same yellow palette regardless of staging
            # side (porcelain only emits these as staged: X=R/C, Y=.).
            if char in ("R", "C"):
                state = STATE_RENAMED
            result[path] = GitStatus(
                path=path,
                char=char,
                state=state,
                tooltip=_tooltip_for(char, state),
                orig_path=orig_path,
            )

        elif rec.startswith("u "):
            parts = rec.split(" ", 10)
            if len(parts) < 11:
                continue
            path = parts[10]
            result[path] = GitStatus(
                path=path,
                char="U",
                state=STATE_CONFLICTED,
                tooltip=_tooltip_for("U", STATE_CONFLICTED),
            )

        elif rec.startswith("? "):
            path = rec[2:]
            result[path] = GitStatus(
                path=path,
                char="?",
                state=STATE_UNTRACKED,
                tooltip=_tooltip_for("?", STATE_UNTRACKED),
            )

        elif rec.startswith("! "):
            path = rec[2:]
            result[path] = GitStatus(
                path=path,
                char="!",
                state=STATE_IGNORED,
                tooltip=_tooltip_for("!", STATE_IGNORED),
            )

        # Anything else (unknown prefix from a future git version) is dropped
        # rather than crashing — favors forward compatibility over strictness.

    return result


# ---------------------------------------------------------------------------
# Directory aggregation — synthesize ancestor entries for changed paths.
# ---------------------------------------------------------------------------

# State priority for picking the "dominant" colour of a directory aggregate.
# Higher value = more attention-worthy. Conflicts trump everything (the user
# MUST resolve them); unstaged trumps staged (the more recent action wins,
# same rationale as the worktree-precedence rule in `_classify_xy`).
_STATE_PRIORITY: dict[str, int] = {
    STATE_IGNORED: 0,
    STATE_UNTRACKED: 1,
    STATE_RENAMED: 2,
    STATE_STAGED: 3,
    STATE_UNSTAGED: 4,
    STATE_CONFLICTED: 5,
}


def _add_directory_aggregates(file_map: dict[str, GitStatus]) -> dict[str, GitStatus]:
    """Return a new map containing file entries PLUS one aggregate per ancestor.

    Each aggregate uses ``char='·'`` (middle dot) and the highest-priority
    state of its descendants. The tooltip is ``"N file(s) changed"`` for
    discoverability. Computed eagerly after every parse — cheap because N is
    typically tens, and depth rarely exceeds 10. Aggregating per call would
    be O(rows × depth) every paint frame; this pre-computation collapses it
    to O(1) hash lookup per ``statusForPath`` call (the contract the FM
    expects per its brief).
    """
    if not file_map:
        return {}

    result = dict(file_map)
    # ancestor path → (descendant count, dominant state)
    dir_info: dict[str, tuple[int, str]] = {}

    for path, status in file_map.items():
        parts = Path(path).parts
        # Walk every strict ancestor: "src/foo/bar.py" → "src/foo", "src".
        # `range(1, len(parts))` excludes the leaf, which is the file itself.
        for depth in range(1, len(parts)):
            ancestor = str(Path(*parts[:depth]))
            count, current_state = dir_info.get(ancestor, (0, STATE_IGNORED))
            count += 1
            if _STATE_PRIORITY.get(status.state, -1) > _STATE_PRIORITY.get(
                current_state, -1
            ):
                current_state = status.state
            dir_info[ancestor] = (count, current_state)

    for dir_path, (count, state) in dir_info.items():
        plural = "" if count == 1 else "s"
        # If a file happens to share a path with an ancestor (impossible in
        # real git output, but defensively) the file entry wins — we don't
        # overwrite. The directory aggregate only fills paths NOT in file_map.
        if dir_path in file_map:
            continue
        result[dir_path] = GitStatus(
            path=dir_path,
            char="·",
            state=state,
            tooltip=f"{count} file{plural} changed",
        )

    return result


# ---------------------------------------------------------------------------
# GitController — the Qt facade. Worker thread + watcher + debounce.
# ---------------------------------------------------------------------------

# Tunables. 200ms debounce is conservative — git index writes are fast and
# bursty (e.g., `git commit` rewrites index AND HEAD AND refs/heads/<branch>
# within milliseconds). Coalescing to one scan per burst is what the FM brief
# explicitly recommends.
_DEBOUNCE_MS = 200
# Timeouts protect against pathological repos (e.g., a corrupted index, a
# `core.fsmonitor` daemon that hangs). The status scan can take seconds on
# very large monorepos; the rev-parse should be near-instant.
_RESOLVE_TIMEOUT_SEC = 5.0
_SCAN_TIMEOUT_SEC = 10.0


class GitController(QObject):
    """Async git-status provider + future git-operations surface.

    Threading model:
      - The owner thread (GUI thread, where QML lives) constructs this
        object, mutates ``repoRoot``, and reads ``statusForPath``.
      - A daemon worker thread runs the slow ops (``git rev-parse`` +
        ``git status``) so the GUI never blocks on git.
      - ``QFileSystemWatcher`` lives on the GUI thread (set as parent); its
        ``fileChanged`` slot starts a debounced ``QTimer`` whose timeout
        wakes the worker via a ``threading.Event``.
      - The worker emits ``statusChanged`` cross-thread; receivers in
        ``AppController`` and QML use ``Qt.QueuedConnection`` per project
        standards §4 P2.

    Lifecycle:
      - Construct on the GUI thread (typically from ``AppController.__init__``).
      - Bind ``repoRoot`` to the current project (e.g. via
        ``set_repo_root(project_path)`` driven by the
        ``StatusBarState.project`` capsule).
      - Call ``stop()`` at app teardown — sets ``_stop_event``, wakes the
        worker so it exits its wait, and joins the thread (≤1s).

    Project-standards anchors:
      §1 P0 — worker is ``daemon=True`` AND owns ``_stop_event``.
      §4 P2 — cross-thread signal connections use explicit ``Qt.QueuedConnection``
              with a grep-able comment at the connect site (responsibility of
              the receiver, e.g. AppController).
      gotcha #10 — GC is suspended around the cross-thread ``statusChanged``
              emit because the worker is constructing dicts under Python
              3.14's aggressive incremental GC.
    """

    statusChanged = Signal()
    repoRootChanged = Signal()
    # Internal worker→GUI signal that asks the GUI thread to rebuild the
    # QFileSystemWatcher entries for a newly-resolved repo root. Connected
    # with explicit QueuedConnection in __init__ — the watcher is GUI-thread-
    # owned and must never be touched from the worker.
    _watcherRefreshRequested = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

        # `repoRoot` is the path the controller is ASKED to watch (typically
        # the project root from the nvim capsule). `_resolved_root` is what
        # `git rev-parse --show-toplevel` returned — they differ when the
        # asked-for path is a subdir of a repo, a worktree, or a submodule.
        # `statusForPath` does its relative-path conversion against
        # `_resolved_root`, not `_repo_root`.
        self._repo_root: str = ""
        self._resolved_root: str = ""
        self._status_map: dict[str, GitStatus] = {}

        # Guards `_status_map` and `_resolved_root` against the worker
        # mutating them while the GUI thread reads in `statusForPath`.
        self._lock = threading.Lock()

        # Worker lifecycle.
        self._stop_event = threading.Event()
        self._scan_wakeup = threading.Event()
        self._worker = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="git-status-worker",
        )
        self._worker.start()

        # File watcher — lives on the GUI thread (parent=self). Re-armed on
        # every event because git's atomic-replace pattern unlinks the watched
        # inode (see `.git/index.lock → .git/index` rename).
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_watched_changed)

        # Debounce: bursts of file events (a single `git commit` touches
        # index, HEAD, MERGE_HEAD, refs/heads/<branch> in quick succession)
        # collapse to one scan.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._wake_worker)

        # Worker→GUI marshaling for watcher rebuild. Explicit QueuedConnection
        # is the project-standards §4 P2 pattern — worker emits, GUI slot runs.
        self._watcherRefreshRequested.connect(
            self._refresh_watcher_for_root,
            Qt.ConnectionType.QueuedConnection,
        )

    # -- QML-facing API ----------------------------------------------------

    @Property(str, notify=repoRootChanged)
    def repoRoot(self) -> str:
        return self._repo_root

    def set_repo_root(self, value: str) -> None:
        """Switch the repo being watched. Idempotent on equal values.

        Clears the existing status map immediately (synchronous, so the UI
        reflects the empty state without waiting for the worker) and asks
        the worker to re-resolve + re-scan against the new root.
        """
        if value == self._repo_root:
            return
        self._repo_root = value
        self.repoRootChanged.emit()
        # Tear down old watcher + map synchronously so callers don't see
        # stale data from the previous project for the brief window before
        # the worker produces the new scan.
        self._clear_watcher()
        with self._lock:
            self._status_map = {}
            self._resolved_root = ""
        self.statusChanged.emit()
        self._wake_worker()

    @Slot(str, result="QVariantMap")
    def statusForPath(self, absolute_path: str) -> dict:
        """Look up a path's git status by ABSOLUTE filesystem path.

        Returns an empty ``QVariantMap`` (``{}``) when:
          - we're not in a git repo (``_resolved_root`` is empty),
          - ``absolute_path`` is outside ``_resolved_root``,
          - or the path simply has no status (clean file).

        The FM's contract accepts ``null`` or an empty object identically —
        no badge is rendered. We return ``{}`` rather than ``None`` because
        ``QVariantMap`` marshaling treats ``None`` as a missing return and
        QML sees ``undefined``; ``{}`` is always a defined empty map.
        """
        with self._lock:
            resolved = self._resolved_root
            if not resolved:
                return {}
            try:
                rel = str(Path(absolute_path).relative_to(resolved))
            except ValueError:
                # Path is outside the repo — silent miss is correct (the FM
                # calls this for every visible row, including files in other
                # projects when switching).
                return {}
            status = self._status_map.get(rel)
        if status is None:
            return {}
        result: dict[str, str] = {
            "char": status.char,
            "state": status.state,
            "tooltip": status.tooltip,
        }
        if status.orig_path is not None:
            result["origPath"] = status.orig_path
        return result

    def file_entries(self) -> list[tuple[str, GitStatus]]:
        """Return all non-aggregate entries as ``(absolute_path, status)`` pairs.

        Directory aggregates (``char == '·'``) are filtered out — they belong
        in the file tree's badges, not in the changes panel which is meant to
        be a flat list of *files* the user has touched. Sorted alphabetically
        by repo-relative path so the panel has a stable display order across
        scans (no jitter on bursty re-parses).

        Returns an empty list when ``_resolved_root`` is unset.
        """
        with self._lock:
            resolved = self._resolved_root
            snapshot = list(self._status_map.items())
        if not resolved:
            return []
        out: list[tuple[str, GitStatus]] = []
        for rel, status in snapshot:
            if status.char == "·":
                continue
            out.append((os.path.join(resolved, rel), status))
        out.sort(key=lambda pair: pair[1].path)
        return out

    # -- Lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        """Signal the worker to exit and join it (≤1s).

        Called from ``AppController.shutdown`` so the daemon thread doesn't
        outlive the Qt event loop. Setting both events is what makes the
        worker exit the ``_scan_wakeup.wait()`` blocking call — otherwise
        the thread would sit in the wait forever (daemon threads survive
        but a clean join is the project standard).
        """
        self._stop_event.set()
        self._scan_wakeup.set()
        self._worker.join(timeout=1.0)

    # -- Worker thread -----------------------------------------------------

    def _run_loop(self) -> None:
        """Worker thread main: wait → scan → wait. Never raises."""
        while not self._stop_event.is_set():
            self._scan_wakeup.wait()
            if self._stop_event.is_set():
                return
            self._scan_wakeup.clear()
            try:
                self._do_scan()
            except Exception:  # noqa: BLE001
                # The worker must NEVER crash — the daemon thread dying
                # silently would leave the watcher firing into a void.
                log.exception("git status scan failed")

    def _do_scan(self) -> None:
        """One scan: resolve repo root, run git status, update map."""
        repo_root = self._repo_root
        if not repo_root:
            self._publish({}, "")
            return

        resolved = self._resolve_repo_root(repo_root)
        if not resolved:
            # Not a git repo — clear map, emit (panel hides), no watcher.
            self._publish({}, "")
            return

        new_map = self._run_status(resolved)
        if new_map is None:
            # Scan failed; preserve previous map rather than blink to empty
            # on a transient error (timeout, fsmonitor hiccup).
            return

        new_map = _add_directory_aggregates(new_map)
        self._publish(new_map, resolved)

        # Hand the watcher rebuild back to the GUI thread — QFileSystemWatcher
        # is not thread-safe and lives on `self`'s thread.
        self._watcherRefreshRequested.emit(resolved)

    def _resolve_repo_root(self, asked: str) -> str:
        """Run ``git rev-parse --show-toplevel`` and return the real root.

        Empty string means "not a git repo" — callers branch on truthiness.
        """
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=asked,
                capture_output=True,
                timeout=_RESOLVE_TIMEOUT_SEC,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            log.debug("git rev-parse failed for %s: %s", asked, exc)
            return ""
        if proc.returncode != 0:
            return ""
        return proc.stdout.decode("utf-8", errors="replace").strip()

    def _run_status(self, cwd: str) -> dict[str, GitStatus] | None:
        """Run ``git status --porcelain=v2 -z`` and parse the output.

        Returns ``None`` on subprocess failure so the caller can preserve
        the previous map. Returns an empty dict on a clean tree.
        """
        try:
            proc = subprocess.run(
                [
                    "git",
                    "status",
                    "--porcelain=v2",
                    "-z",
                    "--branch",
                    "--untracked-files=all",
                ],
                cwd=cwd,
                capture_output=True,
                timeout=_SCAN_TIMEOUT_SEC,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            log.warning("git status failed for %s: %s", cwd, exc)
            return None
        if proc.returncode != 0:
            log.warning(
                "git status exited %d for %s: %s",
                proc.returncode,
                cwd,
                proc.stderr.decode("utf-8", errors="replace").strip(),
            )
            return None
        return parse_porcelain_v2(proc.stdout)

    def _publish(self, new_map: dict[str, GitStatus], resolved: str) -> None:
        """Swap the map under the lock and emit statusChanged.

        GC is suspended around the emit because the worker thread is mid-
        cross-thread signal dispatch — same hazard as nvim_backend's
        ``_dispatch_redraw`` per gotcha #10 (Python 3.14 cyclic GC racing
        the Qt receiver-side wrapper allocation).
        """
        with self._lock:
            old_map = self._status_map
            old_root = self._resolved_root
            self._status_map = new_map
            self._resolved_root = resolved

        if old_map != new_map or old_root != resolved:
            # Counting non-aggregate entries for the log line — directory
            # aggregates are an implementation detail, the user cares about
            # how many actual files have changes.
            file_count = sum(1 for s in new_map.values() if s.char != "·")
            if resolved:
                log.info(
                    "git scan: %d file(s) changed in %s",
                    file_count,
                    resolved,
                )
            else:
                log.info("git scan: cleared (not in a repo)")
            gc.disable()
            try:
                self.statusChanged.emit()
            finally:
                gc.enable()

    # -- GUI-thread slots --------------------------------------------------

    @Slot(str)
    def _refresh_watcher_for_root(self, resolved: str) -> None:
        """Install QFileSystemWatcher entries for the given repo root.

        Called via QueuedConnection from the worker's `_watcherRefreshRequested`
        emit, so runs on the GUI thread where `_watcher` lives.
        """
        self._clear_watcher()
        if not resolved:
            return
        git_dir = self._git_dir_for(resolved)
        if git_dir:
            self._install_watcher(git_dir)

    @staticmethod
    def _git_dir_for(repo_root: str) -> str:
        """Return the .git directory path for a repo root.

        For ordinary repos this is ``<repo_root>/.git``. For worktrees and
        submodules the ``.git`` entry is a FILE containing ``gitdir: <path>``
        — we follow it to find the real git dir. Returns empty string when
        nothing usable is found.
        """
        candidate = os.path.join(repo_root, ".git")
        if os.path.isdir(candidate):
            return candidate
        if os.path.isfile(candidate):
            try:
                with open(candidate, encoding="utf-8") as f:
                    content = f.read().strip()
            except OSError:
                return ""
            if content.startswith("gitdir: "):
                gitdir = content[len("gitdir: ") :]
                if not os.path.isabs(gitdir):
                    gitdir = os.path.join(repo_root, gitdir)
                if os.path.isdir(gitdir):
                    return gitdir
        return ""

    def _install_watcher(self, git_dir: str) -> None:
        """Add the four trigger files to the watcher, if they exist."""
        candidates = [
            os.path.join(git_dir, "index"),
            os.path.join(git_dir, "HEAD"),
            os.path.join(git_dir, "MERGE_HEAD"),
        ]
        # Also watch the current branch's ref file so commits to the current
        # branch (which rewrite refs/heads/<branch>) trigger a scan.
        ref_path = self._current_branch_ref(git_dir)
        if ref_path:
            candidates.append(ref_path)

        existing = [p for p in candidates if os.path.exists(p)]
        if existing:
            self._watcher.addPaths(existing)

    @staticmethod
    def _current_branch_ref(git_dir: str) -> str:
        """Read ``.git/HEAD`` and return the path of the current branch's ref.

        On a detached HEAD, HEAD contains a commit hash instead of ``ref: …``
        — we return empty string and skip watching a ref file (the HEAD watch
        still catches movement).
        """
        head_path = os.path.join(git_dir, "HEAD")
        try:
            with open(head_path, encoding="utf-8") as f:
                content = f.read().strip()
        except OSError:
            return ""
        if not content.startswith("ref: "):
            return ""
        rel_ref = content[len("ref: ") :]
        return os.path.join(git_dir, rel_ref)

    def _clear_watcher(self) -> None:
        files = self._watcher.files()
        if files:
            self._watcher.removePaths(files)
        dirs = self._watcher.directories()
        if dirs:
            self._watcher.removePaths(dirs)

    @Slot(str)
    def _on_watched_changed(self, path: str) -> None:
        """Handle a fileChanged event from the watcher.

        Re-arms the watch if the file was atomically replaced (the inode the
        watcher held is now unlinked; the new file at the same path is a
        different inode, so QFileSystemWatcher silently stops firing on it).
        Then starts the debounce timer — bursts of events collapse to one
        scan per burst.
        """
        if os.path.exists(path) and path not in self._watcher.files():
            self._watcher.addPath(path)
        self._debounce.start()

    def _wake_worker(self) -> None:
        """Set the wakeup event so the worker exits its wait and scans."""
        self._scan_wakeup.set()


# ---------------------------------------------------------------------------
# GitStatusListModel — flat projection of GitController for the panel.
# ---------------------------------------------------------------------------


class GitStatusListModel(QAbstractListModel):
    """Flat list of changed files, projected from ``GitController``.

    Filters directory aggregates (``char == '·'``) out — they live in the
    file tree's per-row badges, not in the changes panel which is a flat
    list of *files* the user has touched. Sorted alphabetically by
    repo-relative path for stable ordering across scans.

    Auto-refreshes on the controller's ``statusChanged`` signal. The panel
    binds ``visible: gitStatusList.count > 0`` so the section disappears
    when the tree is clean or we're not in a git repo.

    Roles (kebab-case bytes for QML):

      - ``path``         — absolute filesystem path (for click-to-jump)
      - ``displayName``  — repo-relative path (the row's label)
      - ``statusChar``   — single-character badge ("M", "?", "A", …)
      - ``statusState``  — semantic state name (for color lookup in QML)
      - ``tooltip``      — human-readable hover text
    """

    PathRole = Qt.ItemDataRole.UserRole + 1
    DisplayNameRole = Qt.ItemDataRole.UserRole + 2
    CharRole = Qt.ItemDataRole.UserRole + 3
    StateRole = Qt.ItemDataRole.UserRole + 4
    TooltipRole = Qt.ItemDataRole.UserRole + 5

    countChanged = Signal()

    def __init__(
        self,
        controller: GitController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._items: list[dict[str, str]] = []
        # The controller emits `statusChanged` on the worker thread when a
        # scan completes (the cross-thread emit is wrapped in gc.disable
        # per gotcha #10). Receivers MUST use QueuedConnection to hop onto
        # the GUI thread before mutating the model — direct connection
        # would call `beginResetModel` from the worker, which crashes Qt's
        # model/view invariants.
        # queued: GitController worker → GitStatusListModel GUI (§4 P2)
        self._controller.statusChanged.connect(
            self._refresh,
            Qt.ConnectionType.QueuedConnection,
        )

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.PathRole: b"path",
            self.DisplayNameRole: b"displayName",
            self.CharRole: b"statusChar",
            self.StateRole: b"statusState",
            self.TooltipRole: b"tooltip",
        }

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008, ARG002
        return len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._items):
            return None
        item = self._items[index.row()]
        if role == self.PathRole:
            return item["path"]
        if role == self.DisplayNameRole:
            return item["displayName"]
        if role == self.CharRole:
            return item["char"]
        if role == self.StateRole:
            return item["state"]
        if role == self.TooltipRole:
            return item["tooltip"]
        return None

    @Property(int, notify=countChanged)
    def count(self) -> int:
        """Row count exposed as a bindable property.

        QML's `ListView.count` exists but isn't directly bindable in older
        Qt versions; exposing our own `count` Property with `countChanged`
        notify makes `visible: model.count > 0` work universally.
        """
        return len(self._items)

    @Slot()
    def _refresh(self) -> None:
        """Rebuild items from the controller and emit modelReset.

        Runs on the GUI thread (the controller's signal is queued). A full
        reset is correct here because we don't know which entries moved;
        for typical change-set sizes (tens of files) a reset is cheaper
        than computing a diff, and the panel re-binds in one pass.
        """
        new_items: list[dict[str, str]] = []
        for abs_path, status in self._controller.file_entries():
            new_items.append(
                {
                    "path": abs_path,
                    "displayName": status.path,
                    "char": status.char,
                    "state": status.state,
                    "tooltip": status.tooltip,
                }
            )
        if new_items == self._items:
            # Identical map — common when an unrelated `.git/` file changes
            # (e.g. fsmonitor cache touch). Skipping the emit avoids a
            # spurious modelReset that would invalidate every visible
            # delegate binding for no reason.
            return
        self.beginResetModel()
        old_count = len(self._items)
        self._items = new_items
        self.endResetModel()
        if old_count != len(self._items):
            self.countChanged.emit()
