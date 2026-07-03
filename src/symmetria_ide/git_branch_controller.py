"""Local-branch + worktree listing for the branches panel (history surface).

The third read-only git provider, sibling of ``git_controller.py`` (working
tree) and ``git_log_controller.py`` (history). Answers "what branches exist,
how do they relate to their upstreams, and which worktree holds each one" —
the panel that lets an agent-driven multi-branch/multi-worktree repo be
understood at a glance.

Three layers, mirroring ``git_log_controller.py``:

  1. Pure parsers — ``parse_for_each_ref_branches`` for the NUL-field
     ``git for-each-ref`` output, ``parse_worktree_list_porcelain`` for
     ``git worktree list --porcelain``, ``format_recency`` for the "3w"-style
     age column, and ``annotate_worktrees`` to join the two. No subprocess,
     no Qt, no I/O.

  2. ``BranchRow`` / ``WorktreeRow`` — normalized rows for the list model.

  3. ``GitBranchController(QObject)`` + ``GitBranchListModel`` — the Qt
     facade. Own daemon worker + queue (a branch refresh must never queue
     behind the log worker, which can hold a ``git show`` for up to 20s),
     same threading/subprocess/emit discipline: ``daemon=True`` worker owning
     a ``_stop_event`` (§1 P0), ``gc.disable()`` around cross-thread emits
     (gotcha #10), parameterless ``Changed`` signals received via
     ``Qt.QueuedConnection`` (§4 P2).

The git invocations and edge-case handling are mined from lazygit
(``pkg/commands/git_commands/{branch_loader,worktree_loader}.go``): one
``for-each-ref`` call yields ahead/behind for ALL branches via
``%(upstream:track)`` (nothing per-branch on the hot path); git warning lines
pollute stdout and are dropped by field count; ``[gone]`` is a literal;
worktree porcelain records omit the ``branch`` line when detached or
mid-rebase; a ``bare`` record is not a worktree and is discarded.
"""

from __future__ import annotations

import gc
import logging
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QModelIndex,
    QObject,
    Qt,
    Signal,
    Slot,
)

log = logging.getLogger(__name__)

# Subprocess timeouts (seconds). Both commands are metadata-only and cheap
# regardless of repo size (no per-branch work), so a short budget suffices.
_RESOLVE_TIMEOUT_SEC = 5.0
_BRANCH_TIMEOUT_SEC = 5.0

# NUL between fields (via %00 in the format), newline between records —
# for-each-ref has no -z, but NUL fields make every legal refname safe
# (git forbids newlines in refnames, so newline records are safe too).
_FIELD_SEP = b"\x00"
_BRANCH_FIELD_COUNT = 7

# The 7 fields we request, in order:
#   %(HEAD)              "*" on the checked-out branch, " " otherwise
#   %(refname:short)     branch name (may surface as "heads/<name>" when a
#                        tag/remote shares the name — stripped in the parser)
#   %(upstream:short)    upstream name, "" when none configured
#   %(upstream:track)    "[ahead N]", "[behind M]", "[ahead N, behind M]",
#                        "[gone]", or "" when up to date / no upstream
#   %(subject)           tip commit subject
#   %(objectname)        tip sha
#   %(committerdate:unix) tip committer time, unix seconds
_BRANCH_FORMAT = (
    "%(HEAD)%00%(refname:short)%00%(upstream:short)%00%(upstream:track)"
    "%00%(subject)%00%(objectname)%00%(committerdate:unix)"
)

_AHEAD_RE = re.compile(r"ahead (\d+)")
_BEHIND_RE = re.compile(r"behind (\d+)")

# Single-unit thresholds for format_recency, largest first. Month is
# lazygit's convention: a twelfth of a 365-day year, so "12M" rolls into
# "1y" exactly.
_YEAR_SEC = 365 * 24 * 3600
_RECENCY_UNITS: tuple[tuple[int, str], ...] = (
    (_YEAR_SEC, "y"),
    (_YEAR_SEC // 12, "M"),
    (7 * 24 * 3600, "w"),
    (24 * 3600, "d"),
    (3600, "h"),
    (60, "m"),
)


@dataclass(slots=True, frozen=True)
class BranchRow:
    """One local branch (or the synthesized detached-HEAD row), normalized.

    ``worktree_path`` is attached by ``annotate_worktrees`` — the absolute
    path of the worktree that has this branch checked out, or ``""`` when no
    worktree holds it. ``detached`` marks the synthesized row for a detached
    HEAD (which never appears in ``refs/heads``, so it can't come from
    ``for-each-ref``).
    """

    name: str
    is_head: bool
    upstream: str
    ahead: int
    behind: int
    upstream_gone: bool
    subject: str
    sha: str
    committer_unix: int
    worktree_path: str = ""
    detached: bool = False


@dataclass(slots=True, frozen=True)
class WorktreeRow:
    """One worktree from ``git worktree list --porcelain``.

    ``branch`` is ``""`` when the worktree is detached OR mid-rebase (git
    omits the ``branch`` line in both cases — the mid-rebase recovery via
    ``rebase-merge/head-name`` that lazygit does is deferred; the row then
    simply annotates no branch).
    """

    path: str
    head_sha: str
    branch: str
    is_bare: bool = False
    is_detached: bool = False


def parse_for_each_ref_branches(blob: bytes) -> list[BranchRow]:
    """Parse ``git for-each-ref --format=<_BRANCH_FORMAT> refs/heads`` stdout.

    Pure function. Records are newline-separated, fields NUL-separated.
    Lines with the wrong field count are skipped — git can interleave warning
    lines into stdout (lazygit issue #1385), and the NUL framing makes real
    records unambiguous.
    """
    rows: list[BranchRow] = []
    for line in blob.split(b"\n"):
        if not line:
            continue
        fields = line.split(_FIELD_SEP)
        if len(fields) != _BRANCH_FIELD_COUNT:
            log.warning(
                "for-each-ref line had %d fields, expected %d — skipped",
                len(fields),
                _BRANCH_FIELD_COUNT,
            )
            continue
        text = [f.decode("utf-8", errors="replace") for f in fields]
        head_marker, name, upstream, track, subject, sha, unix_raw = text
        # `refname:short` yields "heads/<name>" when a tag or remote shares
        # the branch name — normalize defensively (lazygit does the same).
        name = name.removeprefix("heads/")
        ahead_match = _AHEAD_RE.search(track)
        behind_match = _BEHIND_RE.search(track)
        try:
            committer_unix = int(unix_raw)
        except ValueError:
            committer_unix = 0
        rows.append(
            BranchRow(
                name=name,
                is_head=head_marker == "*",
                upstream=upstream,
                ahead=int(ahead_match.group(1)) if ahead_match else 0,
                behind=int(behind_match.group(1)) if behind_match else 0,
                upstream_gone="[gone]" in track,
                subject=subject,
                sha=sha,
                committer_unix=committer_unix,
            )
        )
    return rows


def parse_worktree_list_porcelain(blob: bytes) -> list[WorktreeRow]:
    """Parse ``git worktree list --porcelain`` stdout into worktree rows.

    Pure function. Records are blank-line-separated blocks of
    ``<key> <value>`` lines (``worktree <path>``, ``HEAD <sha>``,
    ``branch refs/heads/<name>``) plus bare flag lines (``bare``,
    ``detached``). A ``bare`` record is the repository itself, not a
    worktree — discarded, matching lazygit.
    """
    rows: list[WorktreeRow] = []
    path = ""
    head_sha = ""
    branch = ""
    is_bare = False
    is_detached = False

    def flush() -> None:
        nonlocal path, head_sha, branch, is_bare, is_detached
        if path and not is_bare:
            rows.append(
                WorktreeRow(
                    path=path,
                    head_sha=head_sha,
                    branch=branch,
                    is_bare=False,
                    is_detached=is_detached,
                )
            )
        path = ""
        head_sha = ""
        branch = ""
        is_bare = False
        is_detached = False

    for raw in blob.split(b"\n"):
        line = raw.decode("utf-8", errors="replace")
        if not line:
            flush()
            continue
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
        elif line.startswith("HEAD "):
            head_sha = line[len("HEAD ") :]
        elif line.startswith("branch "):
            branch = line[len("branch ") :].removeprefix("refs/heads/")
        elif line == "bare":
            is_bare = True
        elif line == "detached":
            is_detached = True
    flush()
    return rows


def format_recency(committer_unix: int, now_unix: int) -> str:
    """Age of a commit as a single-unit string: ``30s`` ``5m`` ``2d`` ``3w``.

    Integer floor per unit, largest unit that fits; ``M`` is 365d/12 so 12
    months roll into ``1y`` exactly (lazygit's convention). Negative or zero
    ages (clock skew, unparsable dates) clamp to ``0s``.
    """
    delta = max(0, now_unix - committer_unix)
    for unit_sec, suffix in _RECENCY_UNITS:
        if delta >= unit_sec:
            return f"{delta // unit_sec}{suffix}"
    return f"{delta}s"


def annotate_worktrees(
    branches: list[BranchRow],
    worktrees: list[WorktreeRow],
    current_root: str,
) -> list[BranchRow]:
    """Join worktrees onto branches; synthesize a detached-HEAD row if needed.

    Pure function. Sets ``worktree_path`` on every branch some worktree has
    checked out (linear scan on ``worktree.branch == branch.name`` — the same
    association lazygit uses). The checked-out branch is moved to index 0
    (lazygit's Head-first rule) — the panel caps its visible height at a few
    rows, and the ``*`` row is its anchor, so it must never scroll out of the
    initial view. When NO branch is head (detached HEAD — such a ref never
    appears in ``refs/heads``), a synthetic row is prepended instead, using
    the sha of the worktree at ``current_root``, so the panel still shows
    where HEAD is without any extra git call.
    """
    by_branch = {wt.branch: wt.path for wt in worktrees if wt.branch}
    annotated = [
        replace(b, worktree_path=by_branch[b.name]) if b.name in by_branch else b
        for b in branches
    ]
    head_index = next((i for i, b in enumerate(annotated) if b.is_head), -1)
    if head_index > 0:
        annotated.insert(0, annotated.pop(head_index))
    if head_index < 0:
        current = next((wt for wt in worktrees if wt.path == current_root), None)
        sha = current.head_sha if current else ""
        annotated.insert(
            0,
            BranchRow(
                name=f"(detached {sha[:8]})" if sha else "(detached)",
                is_head=True,
                upstream="",
                ahead=0,
                behind=0,
                upstream_gone=False,
                subject="",
                sha=sha,
                committer_unix=0,
                worktree_path=current_root if current else "",
                detached=True,
            ),
        )
    return annotated


# ---------------------------------------------------------------------------
# Worker request shape. One kind only: ("branches", asked_root). The root
# travels WITH the request so the worker never reads mutable GUI state
# mid-scan, and the apply step compares it against the live root (race guard).
# ---------------------------------------------------------------------------
_REQ_BRANCHES = "branches"
_STOP = None  # sentinel pushed by stop() to unblock the worker's queue.get()


class GitBranchController(QObject):
    """Async, read-only branch/worktree provider for the branches panel.

    Threading model (mirrors ``GitLogController``): GUI thread constructs and
    calls ``set_repo_root`` / ``reload``; a daemon worker drains ``_queue``
    running ``git rev-parse`` + ``git for-each-ref`` + ``git worktree list``
    so the GUI never blocks on git. Results land under ``_lock``; the
    parameterless ``branchesChanged`` is emitted on the worker thread and
    receivers hop the GUI thread via ``Qt.QueuedConnection`` (§4 P2).

    Deliberately its own worker, NOT extra request kinds on the log worker:
    that worker can hold a pathological ``git show`` for up to 20s, and the
    branches panel must stay fresh independently of diff streaming.
    """

    repoRootChanged = Signal()
    branchesChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

        # `_repo_root` is the path we're ASKED to list branches for (the
        # project anchor); `_resolved_root` is `git rev-parse --show-toplevel`
        # — they differ when the anchor is a subdir. The resolved root is also
        # the "current worktree" key for the checked-out-elsewhere derivation.
        self._repo_root: str = ""
        self._resolved_root: str = ""

        self._branches: list[BranchRow] = []

        # Coalesces redundant reloads — same contract as GitLogController:
        # `reload()` is wired to GitController.workingTreeChanged (200ms
        # debounce window per burst) plus UI pokes; the flag collapses a burst
        # into ONE outstanding fetch. Set under `_lock` in `reload()`, cleared
        # at the TOP of `_do_branches`.
        self._reload_pending: bool = False

        # Guards `_branches`, `_resolved_root`, `_repo_root`, `_reload_pending`.
        self._lock = threading.Lock()

        self._queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._worker = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="git-branch-worker",
        )
        self._worker.start()

    # -- QML-facing properties --------------------------------------------

    @Property(str, notify=repoRootChanged)
    def repoRoot(self) -> str:
        """The RESOLVED repo root (``git rev-parse --show-toplevel``)."""
        with self._lock:
            return self._resolved_root

    def branches(self) -> list[BranchRow]:
        """Snapshot the branch list for the model (GUI thread, under lock)."""
        with self._lock:
            return list(self._branches)

    # -- QML-facing slots --------------------------------------------------

    def set_repo_root(self, value: str) -> None:
        """Switch the repo whose branches are listed. Idempotent on equals.

        Clears the list synchronously (no stale previous-project branches
        while the worker responds) and enqueues a fresh scan. Written under
        the lock so the worker's race-guard read never sees a torn update.
        """
        if value == self._repo_root:
            return
        with self._lock:
            self._repo_root = value
            self._resolved_root = ""
            self._branches = []
            # A switch enqueues its own scan below; a reload pending from the
            # OLD repo must not gate a later reload for the NEW one.
            self._reload_pending = False
        self.repoRootChanged.emit()
        self.branchesChanged.emit()
        if value:
            self._queue.put((_REQ_BRANCHES, value))

    @Slot()
    def reload(self) -> None:
        """Re-scan branches for the current repo. Coalesced like the log's."""
        with self._lock:
            if self._reload_pending or not self._repo_root:
                return
            self._reload_pending = True
            root = self._repo_root
        self._queue.put((_REQ_BRANCHES, root))

    # -- Lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        """Signal the worker to exit and join it (≤1s). Idempotent."""
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        self._queue.put(_STOP)
        self._worker.join(timeout=1.0)

    # -- Worker thread -----------------------------------------------------

    def _run_loop(self) -> None:
        """Worker main: drain the request queue until stopped. Never raises."""
        while not self._stop_event.is_set():
            request = self._queue.get()
            if request is _STOP or self._stop_event.is_set():
                return
            try:
                if request[0] == _REQ_BRANCHES:
                    self._do_branches(asked_root=request[1])
            except Exception:  # noqa: BLE001
                # The worker must NEVER crash — a dead daemon thread would
                # leave every future request stuck in the queue forever.
                log.exception("git-branch worker request failed: %r", request)

    def _do_branches(self, *, asked_root: str) -> None:
        """Resolve the repo, run both scans, apply under the race guard."""
        # Clear the coalescing flag BEFORE fetching — a reload arriving during
        # this scan must be free to queue a fresh one.
        with self._lock:
            self._reload_pending = False
        resolved = self._resolve_repo_root(asked_root)
        if resolved:
            branches = parse_for_each_ref_branches(
                self._run_git(
                    resolved,
                    "for-each-ref",
                    "--sort=-committerdate",
                    f"--format={_BRANCH_FORMAT}",
                    "refs/heads",
                )
            )
            worktrees = parse_worktree_list_porcelain(
                self._run_git(resolved, "worktree", "list", "--porcelain")
            )
            rows = annotate_worktrees(branches, worktrees, resolved)
        else:
            rows = []

        with self._lock:
            # Race guard: a project switch may have landed while git ran.
            if asked_root != self._repo_root:
                return
            old_resolved = self._resolved_root
            self._resolved_root = resolved
            self._branches = rows

        # gc.disable around the cross-thread emits — gotcha #10.
        gc.disable()
        try:
            if old_resolved != resolved:
                self.repoRootChanged.emit()
            self.branchesChanged.emit()
        finally:
            gc.enable()

    # -- Subprocess shells (worker thread only) ----------------------------

    def _resolve_repo_root(self, asked: str) -> str:
        """Run ``git rev-parse --show-toplevel``. Empty string = not a repo."""
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

    def _run_git(self, cwd: str, *args: str) -> bytes:
        """Run one git command, return raw stdout. ``b""`` on any failure."""
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                timeout=_BRANCH_TIMEOUT_SEC,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            log.warning("git %s failed for %s: %s", args[0], cwd, exc)
            return b""
        if proc.returncode != 0:
            log.warning(
                "git %s exited %d for %s: %s",
                args[0],
                proc.returncode,
                cwd,
                proc.stderr.decode("utf-8", errors="replace").strip(),
            )
            return b""
        return proc.stdout


# ---------------------------------------------------------------------------


class GitBranchListModel(QAbstractListModel):
    """Flat list of branches, projected from ``GitBranchController``.

    Auto-refreshes on ``branchesChanged``. Branch lists are small and have no
    paging, so every refresh is a full ``modelReset`` (no append splice).

    Roles: ``name`` ``isHead`` ``upstream`` ``hasUpstream`` ``ahead``
    ``behind`` ``upstreamGone`` ``subject`` ``recency`` ``worktreeName``
    ``checkedOutElsewhere`` ``detached``
    """

    NameRole = Qt.ItemDataRole.UserRole + 1
    IsHeadRole = Qt.ItemDataRole.UserRole + 2
    UpstreamRole = Qt.ItemDataRole.UserRole + 3
    HasUpstreamRole = Qt.ItemDataRole.UserRole + 4
    AheadRole = Qt.ItemDataRole.UserRole + 5
    BehindRole = Qt.ItemDataRole.UserRole + 6
    UpstreamGoneRole = Qt.ItemDataRole.UserRole + 7
    SubjectRole = Qt.ItemDataRole.UserRole + 8
    RecencyRole = Qt.ItemDataRole.UserRole + 9
    WorktreeNameRole = Qt.ItemDataRole.UserRole + 10
    CheckedOutElsewhereRole = Qt.ItemDataRole.UserRole + 11
    DetachedRole = Qt.ItemDataRole.UserRole + 12

    countChanged = Signal()

    def __init__(
        self,
        controller: GitBranchController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._rows: list[BranchRow] = []
        self._resolved_root: str = ""
        # queued: GitBranchController worker → GitBranchListModel GUI (§4 P2)
        self._controller.branchesChanged.connect(
            self._refresh,
            Qt.ConnectionType.QueuedConnection,
        )

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.NameRole: b"name",
            self.IsHeadRole: b"isHead",
            self.UpstreamRole: b"upstream",
            self.HasUpstreamRole: b"hasUpstream",
            self.AheadRole: b"ahead",
            self.BehindRole: b"behind",
            self.UpstreamGoneRole: b"upstreamGone",
            self.SubjectRole: b"subject",
            self.RecencyRole: b"recency",
            self.WorktreeNameRole: b"worktreeName",
            self.CheckedOutElsewhereRole: b"checkedOutElsewhere",
            self.DetachedRole: b"detached",
        }

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008 — QModelIndex() default required by Qt; parent unused per QAbstractListModel contract
        return len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        row = self._rows[index.row()]
        if role == self.NameRole:
            return row.name
        if role == self.IsHeadRole:
            return row.is_head
        if role == self.UpstreamRole:
            return row.upstream
        if role == self.HasUpstreamRole:
            return bool(row.upstream) or row.upstream_gone
        if role == self.AheadRole:
            return row.ahead
        if role == self.BehindRole:
            return row.behind
        if role == self.UpstreamGoneRole:
            return row.upstream_gone
        if role == self.SubjectRole:
            return row.subject
        if role == self.RecencyRole:
            # Computed at read time — the list refreshes on every working-tree
            # change, so a live ticking timer is unnecessary.
            if row.committer_unix <= 0:
                return ""
            return format_recency(row.committer_unix, int(time.time()))
        if role == self.WorktreeNameRole:
            return PurePosixPath(row.worktree_path).name if row.worktree_path else ""
        if role == self.CheckedOutElsewhereRole:
            return bool(row.worktree_path) and row.worktree_path != self._resolved_root
        if role == self.DetachedRole:
            return row.detached
        return None

    @Property(int, notify=countChanged)
    def count(self) -> int:
        """Row count as a bindable property (`visible: model.count > 0`)."""
        return len(self._rows)

    @Slot()
    def _refresh(self) -> None:
        """Rebuild rows from the controller (GUI thread; queued signal)."""
        new_rows = self._controller.branches()
        new_root = self._controller.repoRoot
        if new_rows == self._rows and new_root == self._resolved_root:
            return
        old_len = len(self._rows)
        self.beginResetModel()
        self._rows = new_rows
        self._resolved_root = new_root
        self.endResetModel()
        if len(new_rows) != old_len:
            self.countChanged.emit()
