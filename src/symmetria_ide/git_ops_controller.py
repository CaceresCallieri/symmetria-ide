"""Git *mutation/network* operations for the "git" central surface.

The third git controller in the IDE, and the FIRST to mutate the repo. The
other two are read-only comprehension providers:

  - ``GitController``      — debounced working-tree status scanner (the watcher
                             worker; push-driven by ``.git``/worktree changes).
  - ``GitLogController``   — request-driven committed-log + diff reader
                             (a ``queue.Queue`` worker; pull-driven by the UI).

``GitOpsController`` adds ``pull`` / ``push`` — *transport* operations that move
existing commits between local and remote. They are deliberately distinct in
kind from authoring mutations (stage/commit/rebase), which stay in nvim/shell
per the surface's comprehension-not-mutation thesis (see the gitview memory):
sync is navigation, not content creation, so it doesn't undercut "understand
the changes you didn't write".

Threading model — its OWN lane, mirroring ``GitLogController``'s discipline:

  - A daemon worker thread drains a ``queue.Queue`` of ops, runs the (slow,
    network-bound) ``git`` subprocess, and emits the result cross-thread.
    A SEPARATE worker (not the log controller's queue) is load-bearing: a
    multi-second ``git push`` must never block commit-diff navigation queued
    behind it.
  - ``_busy`` (guarded by ``_lock``) is a single-flight gate: a second
    pull/push request while one is in flight is ignored (the "running" toast
    already communicates the op). Set on the GUI thread when a request is
    accepted, cleared on the worker thread when it completes.
  - ``stop()`` sets ``_stop_event``, pushes the ``_STOP`` sentinel to unblock
    the blocking ``get()``, and joins (≤ the op timeout). Satisfies
    project-standards §1 P0 (daemon worker AND explicit shutdown Event).

Signals (the QML/Toast feedback contract):

  - ``operationStarted(op)``          — emitted on the GUI thread the instant a
                                        request is accepted ("Pulling…").
  - ``operationFinished(op, ok, msg)``— emitted on the WORKER thread when the
                                        subprocess returns; receivers connect
                                        with an explicit ``Qt.QueuedConnection``
                                        (project-standards §4 P2). ``msg`` is a
                                        concise human summary of git's output
                                        (the stat line on success, the trimmed
                                        error tail on failure — the thing the
                                        user asked to "see if there's a
                                        problem").

Pull strategy is lazygit's ``auto`` default: rebase iff the repo is configured
to (``pull.rebase`` truthy), else merge — resolved explicitly into a
``--rebase`` / ``--no-rebase`` flag so a divergent pull never trips modern
git's "Need to specify how to reconcile divergent branches" fatal. Push is
plain ``git push`` — NEVER ``--force``.
"""

from __future__ import annotations

import gc
import logging
import threading
from queue import Queue

from PySide6.QtCore import QObject, Signal, Slot

from .git_subprocess import LOCAL_GIT, GitExecutor

log = logging.getLogger(__name__)

# Subprocess timeouts (seconds). Pull/push are network-bound, so they get a
# generous budget — but bounded, so a wedged transport (dead remote, auth
# hang) eventually fails into the error toast rather than pinning the worker
# forever. `git config`/`rev-parse` are local and fast.
_OP_TIMEOUT_SEC = 120.0
_RESOLVE_TIMEOUT_SEC = 5.0
_CONFIG_TIMEOUT_SEC = 5.0

# Op kinds (also the `op` string carried in the signals + read by QML).
_OP_PULL = "pull"
_OP_PUSH = "push"

# Sentinel pushed by stop() to unblock the worker's blocking queue get().
_STOP = None

# `pull.rebase` values that mean "rebase on pull". Everything else (false,
# unset) means merge — matching lazygit's `auto` mode default.
_REBASE_CONFIG_VALUES = frozenset({"true", "interactive", "merges", "preserve"})


class GitOpsController(QObject):
    """Async ``git pull`` / ``git push`` for the git surface, with status feedback.

    QML-facing surface: ``pull()`` / ``push()`` slots and the
    ``operationStarted`` / ``operationFinished`` signals the toast binds to.
    Identity-stable like the sibling git controllers — the anchored root changes
    internally, the object does not.
    """

    # op name only — fired on the GUI thread when a request is accepted.
    operationStarted = Signal(str)
    # (op, ok, message) — fired on the WORKER thread; connect QUEUED.
    operationFinished = Signal(str, bool, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

        # The path we're asked to operate on (the project anchor). The worker
        # resolves it to the real repo root via `git rev-parse` per op.
        self._repo_root: str = ""
        # Git execution seam (VPS location): LOCAL_GIT or a remote executor
        # injected via set_executor — pull/push then run ON the paired
        # server (its repo, its remotes, its credentials).
        self._executor: GitExecutor = LOCAL_GIT
        # Single-flight gate: True between request-accept and worker-complete.
        self._busy: bool = False

        # Guards `_repo_root` + `_busy` against the worker (clears `_busy`)
        # racing the GUI thread (sets them).
        self._lock = threading.Lock()

        # Worker lifecycle: a request queue + a stop event. The sentinel pushed
        # by stop() is what unblocks the worker's blocking `_queue.get()`.
        self._queue: Queue = Queue()
        self._stop_event = threading.Event()
        self._worker = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="git-ops-worker",
        )
        self._worker.start()

    # NOTE: `_busy` is an INTERNAL single-flight gate only — deliberately NOT
    # exposed as a QML property. A `busy` Q_PROPERTY would need a notify signal
    # emitted from the worker thread, and a QML binding on it would then drive
    # cross-thread binding re-evaluation — the exact hazard `operationFinished`
    # avoids via QueuedConnection. If a future UI needs "busy", expose it with a
    # GUI-thread-marshaled notify, not a raw worker-thread emit.

    # -- Wiring (Python side) ---------------------------------------------

    def set_executor(self, executor) -> None:
        """Swap the git execution seam (VPS location toggle).

        No rescan to trigger — ops are one-shot request/response; the next
        pull/push simply runs through the new executor.
        """
        if executor is self._executor:
            return
        self._executor = executor

    def set_repo_root(self, value: str) -> None:
        """Point ops at a new repo root. Idempotent on equal values.

        Connected (via AppController) to the same ``displayedRootChanged``
        source as the other git controllers, so pull/push target the anchored
        project root even as the raw cwd wanders.
        """
        with self._lock:
            if value == self._repo_root:
                return
            self._repo_root = value

    # -- QML-facing slots --------------------------------------------------

    @Slot()
    def pull(self) -> None:
        """Pull the current branch from its upstream (lazygit ``auto`` mode)."""
        self._dispatch(_OP_PULL)

    @Slot()
    def push(self) -> None:
        """Push the current branch to its upstream (plain ``git push``)."""
        self._dispatch(_OP_PUSH)

    def _dispatch(self, op: str) -> None:
        """Accept-or-reject an op on the GUI thread, then enqueue it.

        Single-flight: a request arriving while another op is in flight is
        dropped (the in-flight "running" toast already says what's happening).
        A request with no repo root resolves immediately to an error result so
        the user gets feedback instead of silence.
        """
        with self._lock:
            if self._busy:
                return
            root = self._repo_root
            if root:
                self._busy = True
        if not root:
            self.operationFinished.emit(
                op, False, "No git repository for this project."
            )
            return
        # Accepted: announce "running" and hand off to the worker. The
        # operationStarted emit is on the GUI thread (direct connection is
        # correct — the cross-thread emit is operationFinished, from the worker).
        self.operationStarted.emit(op)
        self._queue.put((op, root))

    # -- Lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        """Signal the worker to exit and join it.

        Idempotent — a second call returns early rather than re-pushing the
        sentinel and re-joining a dead thread. The join budget is short (2s, in
        the spirit of the read-only workers' ≤1s) ON PURPOSE: ``stop()`` runs on
        the GUI thread during shutdown, so a wedged network op (dead remote,
        auth hang) must NOT block IDE-close for the full 120s op timeout. The
        worker is ``daemon=True``, so the OS reaps it on process exit — and the
        "clean emit at teardown" rationale is moot here (the app is quitting; no
        toast will be shown), unlike the read-only workers whose late emit could
        still touch a live receiver.
        """
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        self._queue.put(_STOP)
        self._worker.join(timeout=2.0)

    # -- Worker thread -----------------------------------------------------

    def _run_loop(self) -> None:
        """Worker main: drain the op queue until stopped. Never raises."""
        while not self._stop_event.is_set():
            request = self._queue.get()
            if request is _STOP or self._stop_event.is_set():
                return
            op, root = request
            ok = False
            message = ""
            try:
                if op == _OP_PULL:
                    ok, message = self._do_pull(root)
                elif op == _OP_PUSH:
                    ok, message = self._do_push(root)
            except Exception:  # noqa: BLE001
                # The worker must NEVER crash — a dead daemon thread would
                # leave `_busy` stuck True and every future op ignored.
                log.exception("git-ops worker request failed: %r", request)
                message = "Internal error running git — see the IDE log."
            finally:
                with self._lock:
                    self._busy = False
                # gc.disable around the cross-thread emit — gotcha #10 (Python
                # 3.14 cyclic GC racing the Qt receiver-side wrapper allocation
                # while the worker is mid signal-dispatch).
                gc.disable()
                try:
                    self.operationFinished.emit(op, ok, message)
                finally:
                    gc.enable()

    def _do_pull(self, asked_root: str) -> tuple[bool, str]:
        """Run ``git pull --no-edit <rebase-flag>`` and summarise the result."""
        resolved = self._resolve_repo_root(asked_root)
        if not resolved:
            return (False, "Not a git repository.")
        rebase_flag = self._pull_rebase_flag(resolved)
        rc, out, err = self._run_git(resolved, ["pull", "--no-edit", rebase_flag])
        combined = _join_streams(out, err)
        if rc == 0:
            if "already up to date" in combined.lower():
                return (True, "Already up to date.")
            return (True, _tail_lines(combined, 3) or "Pull complete.")
        return (False, _tail_lines(combined, 5) or "Pull failed.")

    def _do_push(self, asked_root: str) -> tuple[bool, str]:
        """Run ``git push`` (never ``--force``) and summarise the result."""
        resolved = self._resolve_repo_root(asked_root)
        if not resolved:
            return (False, "Not a git repository.")
        rc, out, err = self._run_git(resolved, ["push"])
        combined = _join_streams(out, err)
        if rc == 0:
            if "everything up-to-date" in combined.lower():
                return (True, "Everything up-to-date.")
            return (True, _tail_lines(combined, 3) or "Push complete.")
        return (False, _tail_lines(combined, 5) or "Push failed.")

    # -- Subprocess shells (worker thread only) ----------------------------

    def _resolve_repo_root(self, asked: str) -> str:
        """Run ``git rev-parse --show-toplevel``. Empty string = not a repo."""
        rc, out, _err = self._run_git(
            asked, ["rev-parse", "--show-toplevel"], timeout=_RESOLVE_TIMEOUT_SEC
        )
        if rc != 0:
            return ""
        return out.strip()

    def _pull_rebase_flag(self, cwd: str) -> str:
        """Resolve lazygit's ``auto`` pull mode into an explicit git flag.

        ``--rebase`` iff ``pull.rebase`` is configured truthy, else
        ``--no-rebase``. Resolving it explicitly (rather than letting bare
        ``git pull`` consult the config) means a divergent pull never trips
        git's "Need to specify how to reconcile divergent branches" fatal —
        it merges by default, exactly as lazygit does.
        """
        rc, out, _err = self._run_git(
            cwd, ["config", "--get", "pull.rebase"], timeout=_CONFIG_TIMEOUT_SEC
        )
        if rc == 0 and out.strip().lower() in _REBASE_CONFIG_VALUES:
            return "--rebase"
        return "--no-rebase"

    def _run_git(
        self, cwd: str, args: list[str], timeout: float = _OP_TIMEOUT_SEC
    ) -> tuple[int, str, str]:
        """Run a git subprocess. Returns ``(returncode, stdout, stderr)``.

        Returns ``(-1, "", <reason>)`` when git can't be run at all (missing
        binary, timeout, OS error) so callers branch on ``rc != 0`` uniformly
        and the reason surfaces in the error toast.

        Deliberate simplification: the executor collapses timeout / missing
        binary / dead transport into one ``None``, so the toast no longer
        distinguishes "timed out after Ns" from other exec failures (the
        pre-executor code did). The specifics are in the log; don't re-add
        per-cause toasts without threading a reason enum through
        ``GitExecutor.execute``.
        """
        proc = self._executor.execute(cwd, list(args), timeout=timeout)
        if proc is None:
            # Exec/transport failure (missing binary, timeout, dead ssh) —
            # the executor already logged the specifics.
            return (-1, "", f"Could not run git {args[0]} (see log).")
        return (
            proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
        )


# -- Pure helpers (module-level — no self, trivially testable) -------------

# Soft cap on a toast message: the card wraps at ~440px, so a runaway hint
# block (git's multi-line "hint:" guidance) is trimmed to stay readable.
_MESSAGE_CHAR_CAP = 400


def _join_streams(stdout: str, stderr: str) -> str:
    """Merge a git command's two streams for summarisation.

    git splits its output unhelpfully: pull writes its stat to stdout, push
    writes ``To <remote>`` + ``branch -> branch`` (and every error) to stderr.
    A single combined view lets one tail-extractor serve both ops.
    """
    parts = [s for s in (stdout, stderr) if s and s.strip()]
    return "\n".join(parts)


def _tail_lines(text: str, max_lines: int) -> str:
    """Last ``max_lines`` non-empty lines of ``text``, capped to a readable size.

    The tail is where git puts the headline: the ``N files changed`` stat after
    a pull, the ``branch -> branch`` line after a push, the ``! [rejected]`` +
    reason after a failure. Leading progress/context lines are dropped.
    """
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    msg = "\n".join(lines[-max_lines:])
    if len(msg) > _MESSAGE_CHAR_CAP:
        msg = msg[: _MESSAGE_CHAR_CAP - 1].rstrip() + "…"
    return msg
