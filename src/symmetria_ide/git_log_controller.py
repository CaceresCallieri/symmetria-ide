"""Git *history* parsing + the GitLogController Qt facade.

The read-only counterpart to ``git_controller.py``. Where ``GitController``
answers "what is uncommitted right now" (working-tree status, watcher-driven),
this module answers "how did the repo get here" (committed history,
request-driven) — the surface that matters most in an agent-driven workflow
where agents keep the working tree clean by committing, so comprehension has
to be reconstructed from the log.

Three layers, top-down:

  1. ``parse_git_log(blob: bytes) -> list[CommitRow]`` — a pure parser for the
     NUL-separated ``git log -z --pretty=format:<...>`` stdout we emit below.
     No subprocess, no Qt, no I/O. Feed it bytes, get an ordered commit list.

  2. ``CommitRow`` — one commit, normalized for the list model. Carries the
     ``agent_id`` (parsed from a ``Symmetria-Agent-Id`` commit trailer) and
     ``seen`` fields NOW, rendered nowhere yet — the seams for the two north
     stars (agent-session correlation + a tracked review frontier). v0 leaves
     both unpopulated/unpersisted.

  3. ``GitLogController(QObject)`` + ``GitLogListModel(QAbstractListModel)`` —
     the Qt facade. A daemon worker thread drains a ``queue.Queue`` of typed
     requests (load a log page, append the next page, load one commit's diff,
     or load one uncommitted file's working-tree diff for the changes sub-view),
     mirroring ``GitController``'s threading/subprocess/emit discipline:
     ``daemon=True`` worker owning a ``_stop_event`` (project-standards §1 P0),
     ``gc.disable()`` around every cross-thread signal emit (gotcha #10), and a
     parameterless ``Changed`` signal whose receivers hop the GUI thread via
     ``Qt.QueuedConnection`` (§4 P2).

Distinct from ``GitController`` by design: history is one-shot-per-request, so
there is no file watcher and no debounce. The request carries the repo root and
the apply step is guarded against ``req_root == self._repo_root`` so a slow scan
from a previously-anchored repo never lands in the model after a project switch.
"""

from __future__ import annotations

import gc
import logging
import queue
import subprocess
import threading
from dataclasses import dataclass

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QModelIndex,
    QObject,
    Qt,
    Signal,
    Slot,
)

from .git_subprocess import resolve_repo_root, run_git

log = logging.getLogger(__name__)

# Subprocess timeouts (seconds). `git show` gets the longest budget — an agent
# commit touching many files produces a large patch that takes a beat to emit.
# (Repo-root resolution uses git_subprocess.resolve_repo_root's own default.)
_LOG_TIMEOUT_SEC = 10.0
_SHOW_TIMEOUT_SEC = 20.0
# A single file's `git diff HEAD -- <path>` is bounded to that file, so it's
# cheaper than a whole-commit `git show`; a tighter budget keeps a wedged diff
# from holding the worker.
_FILE_DIFF_TIMEOUT_SEC = 10.0

# How many commits one page request loads. "Load more" appends the next page.
_PAGE_SIZE = 100

# Soft cap on a single commit's rendered diff. A pathological commit (a vendored
# dependency drop, a generated-file churn) can produce megabytes of patch text
# that would stall the QML text layout. We truncate with a visible notice rather
# than freeze the detail pane. Raise if real commits hit it legitimately.
_DIFF_CHAR_CAP = 400_000

# Field separator (US, 0x1f) between the formatted fields of one commit, and
# the record separator git itself inserts between commits when `-z` is passed
# (NUL, 0x00). Both are control characters that cannot appear in commit
# metadata, so the parse needs no quoting/escaping dance.
_FIELD_SEP = b"\x1f"
_RECORD_SEP = b"\x00"

# The 11 fields we request, in order. Kept as a module constant so the parser's
# unpacking and the format string below can't drift apart.
#   %H  full hash        %h  abbrev hash      %P  parent hashes (space-sep)
#   %an author name      %ae author email     %aI author date (strict ISO)
#   %ar relative date    %s  subject          %b  body
#   %D  ref names        <trailers: Symmetria-Agent-Id value(s)>
_LOG_FIELD_COUNT = 11
_LOG_FORMAT = (
    "%H%x1f%h%x1f%P%x1f%an%x1f%ae%x1f%aI%x1f%ar%x1f%s%x1f%b%x1f%D%x1f"
    "%(trailers:key=Symmetria-Agent-Id,valueonly,separator=%x20)"
)


@dataclass(slots=True, frozen=True)
class CommitRow:
    """One commit, normalized for the history list model.

    ``additions``/``deletions`` default to 0 and are NOT populated in v0 — the
    log listing carries metadata only; per-commit line counts are shown in the
    detail view (from ``git show``) and the list-level totals are a deferred
    enhancement. The roles exist so the model schema is forward-compatible.

    ``agent_id`` is the value of a ``Symmetria-Agent-Id`` commit trailer when
    present (empty otherwise) — the agent-correlation seam. ``seen`` is the
    review-frontier flag, always ``False`` in v0 (no persistence yet).
    """

    hash: str
    abbrev_hash: str
    parents: tuple[str, ...]
    author_name: str
    author_email: str
    author_date_iso: str
    relative_date: str
    subject: str
    body: str
    refs: str
    agent_id: str = ""
    additions: int = 0
    deletions: int = 0
    seen: bool = False


def parse_git_log(blob: bytes) -> list[CommitRow]:
    """Parse ``git log -z --pretty=format:<_LOG_FORMAT>`` stdout into rows.

    Pure function — no subprocess, no Qt. Commits are NUL-separated; fields
    within a commit are 0x1f-separated. Malformed records (wrong field count)
    are skipped with a warning rather than crashing the scan.
    """
    rows: list[CommitRow] = []
    for record in blob.split(_RECORD_SEP):
        if not record:
            # Trailing empty chunk after the final separator, or a stray NUL.
            continue
        fields = record.split(_FIELD_SEP)
        if len(fields) < _LOG_FIELD_COUNT:
            log.warning(
                "git log record had %d fields, expected %d — skipped",
                len(fields),
                _LOG_FIELD_COUNT,
            )
            continue
        text = [f.decode("utf-8", errors="replace") for f in fields[:_LOG_FIELD_COUNT]]
        (
            full_hash,
            abbrev,
            parents_raw,
            an,
            ae,
            aiso,
            ar,
            subject,
            body,
            refs,
            trailer,
        ) = text
        rows.append(
            CommitRow(
                hash=full_hash,
                abbrev_hash=abbrev,
                parents=tuple(p for p in parents_raw.split(" ") if p),
                author_name=an,
                author_email=ae,
                author_date_iso=aiso,
                relative_date=ar,
                subject=subject,
                body=body.rstrip("\n"),
                refs=refs,
                agent_id=trailer.strip(),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Worker request shapes. A small typed tuple is enough — the queue carries
# ("log", asked_root, skip, asked_ref), ("diff", resolved_root, commit_hash),
# and ("file_diff", resolved_root, rel_path, untracked). The repo root AND the
# ref filter travel WITH the request so the worker never reads mutable GUI
# state mid-scan, and the apply step can compare both against the live values
# (race guard) — a fast branch hop must not let branch-A's page land in
# branch-B's list.
# ---------------------------------------------------------------------------
_REQ_LOG = "log"
_REQ_DIFF = "diff"
_REQ_FILE_DIFF = "file_diff"  # one uncommitted file's working-tree diff
_STOP = None  # sentinel pushed by stop() to unblock the worker's queue.get()


class GitLogController(QObject):
    """Async, read-only git-history provider for the history surface.

    Threading model (mirrors ``GitController``):
      - The GUI thread constructs this, calls ``set_repo_root`` / ``reload`` /
        ``load_more`` / ``request_diff``, and reads the exposed properties.
      - A daemon worker thread drains ``_queue`` running ``git rev-parse`` +
        ``git log`` + ``git show`` so the GUI never blocks on git.
      - The worker stores results under ``_lock`` and emits a parameterless
        ``logChanged`` / ``commitDiffChanged``; receivers use
        ``Qt.QueuedConnection`` to hop onto the GUI thread (§4 P2).

    Lifecycle: construct on the GUI thread, bind ``set_repo_root`` to the
    project anchor (same source that drives ``GitController``), call ``stop()``
    at teardown (sets ``_stop_event``, pushes the sentinel, joins ≤1s).
    """

    repoRootChanged = Signal()
    refChanged = Signal()
    logChanged = Signal()
    hasMoreChanged = Signal()
    commitDiffChanged = Signal()
    # Emitted when the working-tree per-file diff fields change. Kept separate
    # from `commitDiffChanged` so the working-tree detail pane and the commit
    # detail pane each bind only the diff they render — a commit-diff fetch
    # never re-binds the file-diff pane and vice versa.
    fileDiffChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

        # `_repo_root` is the path we're ASKED to show history for (the project
        # anchor). `_resolved_root` is what `git rev-parse --show-toplevel`
        # returned — they differ when the anchor is a subdir/worktree. The
        # worker resolves; `repoRoot` exposes the resolved value to QML.
        self._repo_root: str = ""
        self._resolved_root: str = ""

        # Ref the log is filtered to (a local branch name from the branches
        # panel). Empty = HEAD — the exact pre-filter behavior. Guarded by
        # `_lock`; travels with each _REQ_LOG so the apply-side race guard can
        # drop pages fetched for a previously-selected ref.
        self._ref: str = ""

        self._commits: list[CommitRow] = []
        self._has_more: bool = False
        self._current_diff_hash: str = ""
        self._current_diff_text: str = ""
        # Working-tree per-file diff (the uncommitted-files sub-view). Keyed by
        # the file's repo-relative path — the same string QML passes to
        # `request_working_diff` and compares against `currentFileDiffPath` to
        # gate "is this diff current for the selected row?".
        self._current_file_diff_path: str = ""
        self._current_file_diff_text: str = ""

        # Coalesces redundant page-0 reloads. `reload()` is wired to
        # GitController.workingTreeChanged (see AppController.__init__), which
        # fires once per 200ms debounce window on EVERY working-tree edit, not
        # just HEAD moves. Flag-gating `reload()` collapses a burst of fires
        # into ONE outstanding `git log` fetch instead of queueing N identical
        # subprocesses behind the single worker. Set under `_lock` in
        # `reload()`, cleared at the TOP of `_do_log` so a change arriving while
        # a fetch is in flight still re-arms a fresh reload.
        self._reload_pending: bool = False

        # Guards `_commits`, `_resolved_root`, `_has_more`, the diff fields, and
        # `_reload_pending` against the worker mutating them while the GUI
        # thread reads.
        self._lock = threading.Lock()

        # Worker lifecycle: a request queue + a stop event. The sentinel pushed
        # by stop() is what unblocks the worker's blocking `_queue.get()`.
        self._queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._worker = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="git-log-worker",
        )
        self._worker.start()

    # -- QML-facing properties --------------------------------------------

    @Property(str, notify=repoRootChanged)
    def repoRoot(self) -> str:
        """The RESOLVED repo root (``git rev-parse --show-toplevel``)."""
        with self._lock:
            return self._resolved_root

    @Property(str, notify=refChanged)
    def currentRef(self) -> str:
        """The ref the log is filtered to (empty = HEAD, unfiltered)."""
        with self._lock:
            return self._ref

    @Property(bool, notify=hasMoreChanged)
    def hasMore(self) -> bool:
        """True when ``load_more`` would fetch additional older commits."""
        with self._lock:
            return self._has_more

    @Property(str, notify=commitDiffChanged)
    def currentDiffHash(self) -> str:
        """Hash of the commit whose diff ``currentDiffText`` currently holds."""
        with self._lock:
            return self._current_diff_hash

    @Property(str, notify=commitDiffChanged)
    def currentDiffText(self) -> str:
        """Unified-diff text of the selected commit (patch only, no header)."""
        with self._lock:
            return self._current_diff_text

    @Property(str, notify=fileDiffChanged)
    def currentFileDiffPath(self) -> str:
        """Repo-relative path whose diff ``currentFileDiffText`` currently holds."""
        with self._lock:
            return self._current_file_diff_path

    @Property(str, notify=fileDiffChanged)
    def currentFileDiffText(self) -> str:
        """Working-tree unified-diff of the selected uncommitted file (vs HEAD)."""
        with self._lock:
            return self._current_file_diff_text

    def commits(self) -> list[CommitRow]:
        """Snapshot the commit list for the model (GUI thread, under lock)."""
        with self._lock:
            return list(self._commits)

    # -- QML-facing slots --------------------------------------------------

    def set_repo_root(self, value: str) -> None:
        """Switch the repo whose history is shown. Idempotent on equal values.

        Clears the commit list synchronously (so the UI doesn't show the
        previous project's history for the window before the worker responds)
        and enqueues a fresh page-0 load.

        ``_resolved_root`` is cleared under the lock too — not just for the
        ``repoRoot`` property to read empty during the switch, but because
        ``request_diff`` keys its race guard off ``_resolved_root``: leaving
        the old value live would let an in-flight diff request for the old
        repo (or a stale `git show` cwd) slip through. The worker repopulates
        it on the next ``_do_log``. ``_repo_root`` is written under the lock
        as well so the worker's race-guard read in ``_do_log`` never sees a
        torn update — keeps every access to the guarded fields lock-consistent.
        """
        if value == self._repo_root:
            return
        with self._lock:
            self._repo_root = value
            self._resolved_root = ""
            # A project switch must land on HEAD — a branch name filtered in
            # the previous repo has no meaning in the new one.
            ref_was_set = bool(self._ref)
            self._ref = ""
            self._commits = []
            self._has_more = False
            self._current_diff_hash = ""
            self._current_diff_text = ""
            self._current_file_diff_path = ""
            self._current_file_diff_text = ""
            # A project switch re-enqueues its own page-0 load below; reset the
            # coalescing flag so a reload pending from the OLD repo can't gate
            # a later reload for the NEW one.
            self._reload_pending = False
        self.repoRootChanged.emit()
        if ref_was_set:
            self.refChanged.emit()
        self.logChanged.emit()
        self.commitDiffChanged.emit()
        self.fileDiffChanged.emit()
        self.hasMoreChanged.emit()
        if value:
            self._queue.put((_REQ_LOG, value, 0, ""))

    @Slot()
    def reload(self) -> None:
        """Re-fetch page 0 for the current repo (replace the commit list).

        Coalesced: if a flag-gated reload is already outstanding, this is a
        no-op. The wire from ``GitController.workingTreeChanged`` fires once per
        debounce window during active editing, so without this gate a burst of
        edits would queue N identical ``git log`` subprocesses behind the single
        worker. The flag is cleared at the top of ``_do_log``, so a change
        arriving while a fetch is in flight still re-arms a fresh reload.
        """
        with self._lock:
            if self._reload_pending or not self._repo_root:
                return
            self._reload_pending = True
            root = self._repo_root
            ref = self._ref
        self._queue.put((_REQ_LOG, root, 0, ref))

    @Slot()
    def load_more(self) -> None:
        """Fetch the next page of older commits and append it.

        No-op when a load is unnecessary (no repo, or the last page already
        returned fewer than ``_PAGE_SIZE`` rows). ``skip`` is the current
        commit count, read under the lock on the GUI thread.
        """
        with self._lock:
            if not self._has_more:
                return
            skip = len(self._commits)
            ref = self._ref
        if self._repo_root:
            self._queue.put((_REQ_LOG, self._repo_root, skip, ref))

    @Slot(str)
    def set_ref(self, value: str) -> None:
        """Filter the log to one ref (a local branch name). Empty = HEAD.

        Idempotent on equal values. Clears the commit list synchronously (so
        the UI never shows branch-A commits labeled as branch-B) plus the
        commit-diff fields — the selected commit may not exist on the new
        ref. The FILE-diff fields stay: they are working-tree-scoped and
        independent of which ref the history list shows. Enqueues a fresh
        page-0 load carrying the new ref.
        """
        if value == self._ref:
            return
        with self._lock:
            self._ref = value
            self._commits = []
            self._has_more = False
            self._current_diff_hash = ""
            self._current_diff_text = ""
            # A ref switch enqueues its own page-0 load below; a reload
            # pending for the OLD ref must not gate a later reload.
            self._reload_pending = False
            root = self._repo_root
        self.refChanged.emit()
        self.logChanged.emit()
        self.commitDiffChanged.emit()
        self.hasMoreChanged.emit()
        if root:
            self._queue.put((_REQ_LOG, root, 0, value))

    @Slot(str)
    def request_diff(self, commit_hash: str) -> None:
        """Load one commit's diff into ``currentDiffText`` (async)."""
        if not commit_hash:
            return
        with self._lock:
            resolved = self._resolved_root
        if resolved:
            self._queue.put((_REQ_DIFF, resolved, commit_hash))

    @Slot(str, bool)
    def request_working_diff(self, rel_path: str, untracked: bool) -> None:
        """Load one uncommitted file's working-tree diff (vs HEAD), async.

        ``rel_path`` is repo-relative (the status model's ``displayName`` role).
        ``untracked`` selects the diff command: a tracked file diffs against
        HEAD, while an untracked file has no HEAD blob so it is rendered as an
        all-additions ``git diff --no-index`` against ``/dev/null`` (see
        ``_run_working_diff``). The path doubles as the race-guard key so a
        stale result for a previously-selected file is dropped on apply.
        """
        if not rel_path:
            return
        with self._lock:
            resolved = self._resolved_root
        if resolved:
            self._queue.put((_REQ_FILE_DIFF, resolved, rel_path, untracked))

    # -- Lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        """Signal the worker to exit and join it (≤1s).

        Sets ``_stop_event`` and pushes the ``_STOP`` sentinel so the worker's
        blocking ``_queue.get()`` returns and the loop sees the stop flag.
        Idempotent — a second call (or a double shutdown path) returns early
        rather than pushing another sentinel and re-joining a dead thread.
        """
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
                kind = request[0]
                if kind == _REQ_LOG:
                    self._do_log(
                        asked_root=request[1],
                        skip=request[2],
                        asked_ref=request[3],
                    )
                elif kind == _REQ_DIFF:
                    self._do_diff(resolved_root=request[1], commit_hash=request[2])
                elif kind == _REQ_FILE_DIFF:
                    self._do_file_diff(
                        resolved_root=request[1],
                        rel_path=request[2],
                        untracked=request[3],
                    )
            except Exception:  # noqa: BLE001
                # The worker must NEVER crash — a dead daemon thread would
                # leave every future request stuck in the queue forever.
                log.exception("git-log worker request failed: %r", request)

    def _do_log(self, *, asked_root: str, skip: int, asked_ref: str) -> None:
        """Resolve the repo, fetch one page, apply it under the race guard."""
        # Clear the coalescing flag BEFORE fetching: any reload request that
        # arrives during this fetch must be free to queue a fresh one (clearing
        # after would drop a change landing mid-flight). Harmless for the
        # load_more/set_repo_root callers, which never set the flag.
        with self._lock:
            self._reload_pending = False
        resolved = resolve_repo_root(asked_root)
        rows = self._run_log(resolved, skip=skip, ref=asked_ref) if resolved else []
        has_more = len(rows) == _PAGE_SIZE

        with self._lock:
            # Race guard: a project switch OR a ref switch may have landed
            # while this scan ran. Dropping the stale result keeps another
            # repo's — or another branch's — commits out of the model.
            # (`set_repo_root`/`set_ref` already cleared + re-enqueued.)
            if asked_root != self._repo_root or asked_ref != self._ref:
                return
            old_resolved = self._resolved_root
            self._resolved_root = resolved
            if skip == 0:
                self._commits = rows
            else:
                self._commits = [*self._commits, *rows]
            old_has_more = self._has_more
            self._has_more = has_more

        # gc.disable around the cross-thread emits — gotcha #10 (Python 3.14
        # cyclic GC racing the Qt receiver-side wrapper allocation while the
        # worker is mid signal-dispatch). One window covers all the emits.
        gc.disable()
        try:
            # Gate each notify on an actual change — a `load_more` append leaves
            # the resolved root (and often hasMore) untouched, so only `logChanged`
            # fires for it.
            if old_resolved != resolved:
                self.repoRootChanged.emit()
            self.logChanged.emit()
            if old_has_more != has_more:
                self.hasMoreChanged.emit()
        finally:
            gc.enable()

    def _do_diff(self, *, resolved_root: str, commit_hash: str) -> None:
        """Fetch one commit's patch and publish it to the diff properties."""
        text = self._run_show(resolved_root, commit_hash)

        with self._lock:
            if resolved_root != self._resolved_root:
                return
            self._current_diff_hash = commit_hash
            self._current_diff_text = text

        gc.disable()
        try:
            self.commitDiffChanged.emit()
        finally:
            gc.enable()

    def _do_file_diff(
        self, *, resolved_root: str, rel_path: str, untracked: bool
    ) -> None:
        """Fetch one uncommitted file's working-tree diff and publish it."""
        text = self._run_working_diff(resolved_root, rel_path, untracked)

        with self._lock:
            # Same race guard as `_do_diff`: a project switch may have landed
            # while git ran. Dropping the stale result keeps another repo's
            # file diff out of the pane.
            if resolved_root != self._resolved_root:
                return
            self._current_file_diff_path = rel_path
            self._current_file_diff_text = text

        gc.disable()
        try:
            self.fileDiffChanged.emit()
        finally:
            gc.enable()

    # -- Subprocess shells (worker thread only) ----------------------------
    # Repo-root resolution + plain run-and-return-stdout commands come from
    # git_subprocess.py (shared with the branch controller). `_run_show` and
    # `_run_working_diff` stay local: their exit-code semantics (accepting 1
    # from `--no-index`) and text post-processing don't fit that contract.

    def _run_log(self, cwd: str, *, skip: int, ref: str = "") -> list[CommitRow]:
        """Run ``git log -z`` for one page and parse it. ``[]`` on failure.

        A non-empty ``ref`` filters the log to that ref's history; the ``--``
        after it disambiguates a branch name from a path. Empty ref keeps the
        argv byte-identical to the pre-filter command (HEAD implicit).
        """
        return parse_git_log(
            run_git(
                cwd,
                "log",
                "-z",
                "--no-color",
                f"--max-count={_PAGE_SIZE}",
                f"--skip={skip}",
                f"--pretty=format:{_LOG_FORMAT}",
                *([ref, "--"] if ref else []),
                timeout=_LOG_TIMEOUT_SEC,
            )
        )

    def _run_show(self, cwd: str, commit_hash: str) -> str:
        """Run ``git show <hash>`` and return the patch text only.

        ``--format=%x00`` emits a single NUL where the commit header would be,
        so everything after the first NUL is the raw patch — a robust way to
        strip git's default decorated header without an empty-format edge case.
        Truncated at ``_DIFF_CHAR_CAP`` with a visible notice.
        """
        try:
            proc = subprocess.run(
                [
                    "git",
                    "show",
                    "--no-color",
                    "--format=%x00",
                    "--patch",
                    commit_hash,
                ],
                cwd=cwd,
                capture_output=True,
                timeout=_SHOW_TIMEOUT_SEC,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            log.warning("git show failed for %s @ %s: %s", commit_hash, cwd, exc)
            return ""
        if proc.returncode != 0:
            log.warning(
                "git show exited %d for %s @ %s: %s",
                proc.returncode,
                commit_hash,
                cwd,
                proc.stderr.decode("utf-8", errors="replace").strip(),
            )
            return ""
        out = proc.stdout.decode("utf-8", errors="replace")
        _, sep, patch = out.partition("\x00")
        text = (patch if sep else out).lstrip("\n")
        if len(text) > _DIFF_CHAR_CAP:
            text = (
                text[:_DIFF_CHAR_CAP]
                + f"\n\n… diff truncated at {_DIFF_CHAR_CAP:,} characters …"
            )
        return text

    def _run_working_diff(self, cwd: str, rel_path: str, untracked: bool) -> str:
        """Run the working-tree diff for one file. ``""`` on failure.

        Tracked files diff against HEAD (``git diff HEAD -- <path>``), which
        combines staged AND unstaged changes into one "what's different from the
        last commit" patch — the comprehension frame the uncommitted-files view
        is built around. Untracked files have no HEAD blob, so ``git diff``
        would emit nothing; we render them as an all-additions diff via
        ``--no-index`` against ``/dev/null`` instead.

        ``--no-index`` returns exit code 1 when the files differ (which is
        always, for a non-empty new file) — that is success, not failure, so we
        accept returncodes 0 and 1 for both forms (plain ``git diff`` without
        ``--exit-code`` returns 0 regardless). Output is truncated at
        ``_DIFF_CHAR_CAP`` with a visible notice, matching ``_run_show``.
        """
        if untracked:
            cmd = [
                "git",
                "diff",
                "--no-color",
                "--no-index",
                "--",
                "/dev/null",
                rel_path,
            ]
        else:
            cmd = ["git", "diff", "--no-color", "HEAD", "--", rel_path]
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                timeout=_FILE_DIFF_TIMEOUT_SEC,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            log.warning("git diff failed for %s @ %s: %s", rel_path, cwd, exc)
            return ""
        # Accepted exit codes differ by form: `git diff HEAD` (tracked path,
        # no `--exit-code`) always returns 0, even when there are changes; the
        # `--no-index` (untracked) form returns 1 on differences — its normal
        # success case. So 0 and 1 are both valid here (1 only ever from the
        # untracked form); anything else is a real error worth logging.
        if proc.returncode not in (0, 1):
            log.warning(
                "git diff exited %d for %s @ %s: %s",
                proc.returncode,
                rel_path,
                cwd,
                proc.stderr.decode("utf-8", errors="replace").strip(),
            )
            return ""
        text = proc.stdout.decode("utf-8", errors="replace").lstrip("\n")
        if len(text) > _DIFF_CHAR_CAP:
            text = (
                text[:_DIFF_CHAR_CAP]
                + f"\n\n… diff truncated at {_DIFF_CHAR_CAP:,} characters …"
            )
        return text


# ---------------------------------------------------------------------------


class GitLogListModel(QAbstractListModel):
    """Flat list of commits, projected from ``GitLogController``.

    Auto-refreshes on the controller's ``logChanged`` signal. A page-0 load
    (or repo switch) is a full ``modelReset``; a "load more" append that simply
    extends the existing prefix uses ``beginInsertRows`` so the ListView keeps
    its scroll position instead of snapping to the top.

    Roles (kebab-case bytes for QML):
      ``hash`` ``abbrevHash`` ``parents`` ``authorName`` ``authorEmail``
      ``dateIso`` ``relativeDate`` ``subject`` ``body`` ``refs`` ``agentId``
      ``additions`` ``deletions`` ``seen``
    """

    HashRole = Qt.ItemDataRole.UserRole + 1
    AbbrevHashRole = Qt.ItemDataRole.UserRole + 2
    ParentsRole = Qt.ItemDataRole.UserRole + 3
    AuthorNameRole = Qt.ItemDataRole.UserRole + 4
    AuthorEmailRole = Qt.ItemDataRole.UserRole + 5
    DateIsoRole = Qt.ItemDataRole.UserRole + 6
    RelativeDateRole = Qt.ItemDataRole.UserRole + 7
    SubjectRole = Qt.ItemDataRole.UserRole + 8
    BodyRole = Qt.ItemDataRole.UserRole + 9
    RefsRole = Qt.ItemDataRole.UserRole + 10
    AgentIdRole = Qt.ItemDataRole.UserRole + 11
    AdditionsRole = Qt.ItemDataRole.UserRole + 12
    DeletionsRole = Qt.ItemDataRole.UserRole + 13
    SeenRole = Qt.ItemDataRole.UserRole + 14

    countChanged = Signal()

    def __init__(
        self,
        controller: GitLogController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._rows: list[CommitRow] = []
        # The controller emits `logChanged` on the worker thread (the emit is
        # gc.disable-guarded per gotcha #10). Receivers MUST use QueuedConnection
        # to hop onto the GUI thread before mutating the model — a direct
        # connection would call begin/endResetModel from the worker and crash
        # Qt's model/view invariants.
        # queued: GitLogController worker → GitLogListModel GUI (§4 P2)
        self._controller.logChanged.connect(
            self._refresh,
            Qt.ConnectionType.QueuedConnection,
        )

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.HashRole: b"hash",
            self.AbbrevHashRole: b"abbrevHash",
            self.ParentsRole: b"parents",
            self.AuthorNameRole: b"authorName",
            self.AuthorEmailRole: b"authorEmail",
            self.DateIsoRole: b"dateIso",
            self.RelativeDateRole: b"relativeDate",
            self.SubjectRole: b"subject",
            self.BodyRole: b"body",
            self.RefsRole: b"refs",
            self.AgentIdRole: b"agentId",
            self.AdditionsRole: b"additions",
            self.DeletionsRole: b"deletions",
            self.SeenRole: b"seen",
        }

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008 — QModelIndex() default required by Qt; ARG002 — parent unused per QAbstractListModel contract
        return len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        row = self._rows[index.row()]
        if role == self.HashRole:
            return row.hash
        if role == self.AbbrevHashRole:
            return row.abbrev_hash
        if role == self.ParentsRole:
            return list(row.parents)
        if role == self.AuthorNameRole:
            return row.author_name
        if role == self.AuthorEmailRole:
            return row.author_email
        if role == self.DateIsoRole:
            return row.author_date_iso
        if role == self.RelativeDateRole:
            return row.relative_date
        if role == self.SubjectRole:
            return row.subject
        if role == self.BodyRole:
            return row.body
        if role == self.RefsRole:
            return row.refs
        if role == self.AgentIdRole:
            return row.agent_id
        if role == self.AdditionsRole:
            return row.additions
        if role == self.DeletionsRole:
            return row.deletions
        if role == self.SeenRole:
            return row.seen
        return None

    @Property(int, notify=countChanged)
    def count(self) -> int:
        """Row count as a bindable property (`visible: model.count > 0`)."""
        return len(self._rows)

    @Slot()
    def _refresh(self) -> None:
        """Rebuild rows from the controller. Append-aware to preserve scroll.

        Runs on the GUI thread (the controller's signal is queued). When the new
        list strictly extends the current one (a "load more" append), we splice
        in only the tail via ``beginInsertRows`` so the ListView's contentY is
        undisturbed. Any other change (fresh load, repo switch, clear) is a full
        ``modelReset``.
        """
        new_rows = self._controller.commits()
        old_len = len(self._rows)
        new_len = len(new_rows)
        is_pure_append = (
            new_len > old_len and old_len > 0 and new_rows[:old_len] == self._rows
        )
        if is_pure_append:
            self.beginInsertRows(QModelIndex(), old_len, new_len - 1)
            self._rows = new_rows
            self.endInsertRows()
        else:
            if new_rows == self._rows:
                return
            self.beginResetModel()
            self._rows = new_rows
            self.endResetModel()
        if new_len != old_len:
            self.countChanged.emit()
