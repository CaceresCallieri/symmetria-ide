"""Tests for `GitOpsController` — the git surface's pull/push (mutation) lane.

Three layers, mirroring the sibling git tests:

  - Pure module-level helpers (`_tail_lines`, `_join_streams`) — no Qt, no
    subprocess, no repo. Same "this input produces this string" style as
    `test_git_log_controller.py`.
  - Real-git integration against a LOCAL bare remote (offline + deterministic)
    for the subprocess-bound logic: the lazygit-`auto` pull-mode resolution,
    the not-a-repo guard, and the push/pull success summaries. Same
    instantiate-in-try/finally-stop() pattern as the `_count_untracked_lines`
    tests in `test_git_controller.py`.
  - Dispatch/worker contract: the single-flight `_busy` gate, the no-repo
    failure emit, and `_busy` recovery after a worker exception. These capture
    signals via an explicit `DirectConnection` (the emit runs in the emitting
    thread, so a `threading.Event` is set synchronously) — NO event loop / NO
    `processEvents`, per the shared-app SEGV memo in `.claude/memory`.

See `src/symmetria_ide/git_ops_controller.py`.
"""

from __future__ import annotations

import os
import subprocess
import threading

from PySide6.QtCore import Qt

from symmetria_ide.git_ops_controller import (
    _MESSAGE_CHAR_CAP,
    GitOpsController,
    _join_streams,
    _tail_lines,
)

# ---------------------------------------------------------------------------
# Pure helpers — _tail_lines
# ---------------------------------------------------------------------------


def test_tail_lines_empty_is_empty() -> None:
    assert _tail_lines("", 3) == ""
    assert _tail_lines("   \n\n  \n", 3) == ""


def test_tail_lines_keeps_only_the_last_n_nonblank() -> None:
    text = "Updating a..b\nFast-forward\n 3 files changed, 10 insertions(+)\n"
    # The headline (the stat) is at the tail; leading context is dropped.
    assert _tail_lines(text, 2) == "Fast-forward\n 3 files changed, 10 insertions(+)"


def test_tail_lines_skips_blank_lines_between_content() -> None:
    text = "first\n\n\nsecond\n\nthird\n"
    assert _tail_lines(text, 2) == "second\nthird"


def test_tail_lines_caps_runaway_message() -> None:
    huge = "x" * (_MESSAGE_CHAR_CAP * 2)
    out = _tail_lines(huge, 1)
    assert len(out) <= _MESSAGE_CHAR_CAP
    assert out.endswith("…")


# ---------------------------------------------------------------------------
# Pure helpers — _join_streams
# ---------------------------------------------------------------------------


def test_join_streams_combines_both() -> None:
    assert _join_streams("out", "err") == "out\nerr"


def test_join_streams_drops_blank_sides() -> None:
    assert _join_streams("only out", "   ") == "only out"
    assert _join_streams("", "only err") == "only err"
    assert _join_streams("  ", "\n") == ""


# ---------------------------------------------------------------------------
# Integration helpers — a work repo cloned from a local bare remote
# ---------------------------------------------------------------------------


def _git(cwd, *args: str) -> None:
    """Run a git command in `cwd`, raising on failure (test setup must succeed)."""
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _make_repo_with_remote(tmp_path):
    """A work tree wired to a LOCAL bare remote, with an upstream-tracking branch.

    No network: the remote is a bare repo on the filesystem. After this returns,
    the work tree is in sync with origin (one pushed commit, upstream set), so a
    bare `git push`/`git pull` reports "up-to-date" until a new commit lands.
    """
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "clone", str(remote), str(work))
    # Local identity + no signing so commits succeed regardless of the host's
    # global git config (which a CI box may leave unset / sign-by-default).
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    _git(work, "config", "commit.gpgsign", "false")
    (work / "f.txt").write_text("hello\n")
    _git(work, "add", "f.txt")
    _git(work, "commit", "-m", "init")
    # Establish the upstream so later bare push/pull have a target.
    _git(work, "push", "-u", "origin", "HEAD")
    return work


# ---------------------------------------------------------------------------
# _pull_rebase_flag — lazygit `auto` mode resolution
# ---------------------------------------------------------------------------


def test_pull_rebase_flag_maps_config_values(tmp_path) -> None:
    work = _make_repo_with_remote(tmp_path)
    controller = GitOpsController()
    try:
        # `--get` returns the most specific (local) value, so these overrides
        # are hermetic regardless of the host's global pull.rebase.
        _git(work, "config", "pull.rebase", "false")
        assert controller._pull_rebase_flag(str(work)) == "--no-rebase"
        _git(work, "config", "pull.rebase", "true")
        assert controller._pull_rebase_flag(str(work)) == "--rebase"
        _git(work, "config", "pull.rebase", "interactive")
        assert controller._pull_rebase_flag(str(work)) == "--rebase"
        _git(work, "config", "pull.rebase", "merges")
        assert controller._pull_rebase_flag(str(work)) == "--rebase"
        _git(work, "config", "pull.rebase", "preserve")
        assert controller._pull_rebase_flag(str(work)) == "--rebase"
    finally:
        controller.stop()


def test_pull_rebase_flag_unset_defaults_to_no_rebase(tmp_path, monkeypatch) -> None:
    # The real-world DEFAULT (no pull.rebase anywhere) must resolve to merge,
    # i.e. --no-rebase (lazygit `auto`). Neutralize the host's global/system git
    # config so "unset" is genuinely unset — otherwise `git config --get
    # pull.rebase` could read the host's global value and make this flaky.
    # `_run_git` shells out without an explicit env, inheriting os.environ,
    # which monkeypatch.setenv mutates for this process.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    work = _make_repo_with_remote(tmp_path)
    controller = GitOpsController()
    try:
        assert controller._pull_rebase_flag(str(work)) == "--no-rebase"
    finally:
        controller.stop()


# ---------------------------------------------------------------------------
# _do_pull / _do_push — guard + success summaries
# ---------------------------------------------------------------------------


def test_do_pull_not_a_repo(tmp_path) -> None:
    # tmp_path is under /tmp — not a git repo — so rev-parse fails and the op
    # returns the guard message rather than running pull against nothing.
    controller = GitOpsController()
    try:
        ok, message = controller._do_pull(str(tmp_path))
        assert ok is False
        assert "Not a git repository" in message
    finally:
        controller.stop()


def test_do_push_not_a_repo(tmp_path) -> None:
    controller = GitOpsController()
    try:
        ok, message = controller._do_push(str(tmp_path))
        assert ok is False
        assert "Not a git repository" in message
    finally:
        controller.stop()


def test_do_pull_already_up_to_date(tmp_path) -> None:
    work = _make_repo_with_remote(tmp_path)
    controller = GitOpsController()
    try:
        ok, message = controller._do_pull(str(work))
        assert ok is True
        assert "up to date" in message.lower()
    finally:
        controller.stop()


def test_do_push_up_to_date_when_in_sync(tmp_path) -> None:
    # Fresh clone is already in sync with origin → "Everything up-to-date."
    work = _make_repo_with_remote(tmp_path)
    controller = GitOpsController()
    try:
        ok, message = controller._do_push(str(work))
        assert ok is True
        assert "up-to-date" in message.lower() or "up to date" in message.lower()
    finally:
        controller.stop()


def test_do_push_publishes_a_new_commit(tmp_path) -> None:
    work = _make_repo_with_remote(tmp_path)
    # A commit the remote doesn't have yet → push succeeds with a non-empty
    # summary (the `branch -> branch` / `To <remote>` tail).
    (work / "f.txt").write_text("hello again\n")
    _git(work, "add", "f.txt")
    _git(work, "commit", "-m", "second")
    controller = GitOpsController()
    try:
        ok, message = controller._do_push(str(work))
        assert ok is True
        assert message  # a concise summary of git's push output
        assert "up-to-date" not in message.lower()
    finally:
        controller.stop()


# ---------------------------------------------------------------------------
# Dispatch / worker contract — the single-flight gate, no-repo emit, and
# busy-recovery-after-exception (the riskiest, previously-untested paths).
# ---------------------------------------------------------------------------


def test_dispatch_with_no_repo_root_emits_failure() -> None:
    # No repo root set → _dispatch emits operationFinished(ok=False)
    # SYNCHRONOUSLY on the calling thread (the GUI-thread no-repo path) without
    # enqueuing work or latching _busy.
    controller = GitOpsController()
    captured: list = []
    controller.operationFinished.connect(
        lambda op, ok, msg: captured.append((op, ok, msg)),
        Qt.ConnectionType.DirectConnection,
    )
    try:
        controller.pull()
        assert len(captured) == 1
        op, ok, _msg = captured[0]
        assert op == "pull"
        assert ok is False
        assert controller._queue.empty()
        with controller._lock:
            assert controller._busy is False
    finally:
        controller.stop()


def test_single_flight_drops_second_request() -> None:
    # While an op is in flight (_busy True), a second dispatch is dropped: no
    # operationStarted fires and nothing is enqueued. A repo root IS set, so
    # the drop is due to the gate (checked first), not the no-repo guard.
    controller = GitOpsController()
    started: list = []
    controller.operationStarted.connect(
        lambda op: started.append(op), Qt.ConnectionType.DirectConnection
    )
    try:
        controller.set_repo_root("/some/repo/path")
        with controller._lock:
            controller._busy = True  # simulate an op already running
        controller.pull()
        assert started == []
        assert controller._queue.empty()
    finally:
        # Release the simulated gate before stop() (no real worker op is
        # running; this is just hygiene so the flag isn't left set).
        with controller._lock:
            controller._busy = False
        controller.stop()


def test_busy_cleared_after_worker_exception(tmp_path, monkeypatch) -> None:
    # If _do_pull raises inside the worker, the finally MUST clear _busy and
    # still emit operationFinished(ok=False) — otherwise the controller wedges
    # (the single-flight gate would drop every future op forever).
    controller = GitOpsController()
    done = threading.Event()
    captured: list = []

    def _capture(op, ok, msg) -> None:
        captured.append((op, ok, msg))
        done.set()

    # DirectConnection → _capture runs in the WORKER thread that emits, so the
    # Event is set without needing a main-thread event loop / processEvents.
    controller.operationFinished.connect(_capture, Qt.ConnectionType.DirectConnection)

    def _boom(_root) -> tuple[bool, str]:
        raise RuntimeError("boom")

    try:
        monkeypatch.setattr(controller, "_do_pull", _boom)
        controller.set_repo_root(str(tmp_path))
        controller.pull()
        assert done.wait(timeout=5.0), "worker never emitted operationFinished"
        op, ok, _msg = captured[0]
        assert op == "pull"
        assert ok is False
        with controller._lock:
            assert controller._busy is False
    finally:
        controller.stop()
