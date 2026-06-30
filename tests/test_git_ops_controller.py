"""Tests for `GitOpsController` — the git surface's pull/push (mutation) lane.

Two layers, mirroring the sibling git tests:

  - Pure module-level helpers (`_tail_lines`, `_join_streams`) — no Qt, no
    subprocess, no repo. Same "this input produces this string" style as
    `test_git_log_controller.py`.
  - Real-git integration against a LOCAL bare remote (offline + deterministic)
    for the subprocess-bound logic: the lazygit-`auto` pull-mode resolution,
    the not-a-repo guard, and the push/pull success summaries. Same
    instantiate-in-try/finally-stop() pattern as the `_count_untracked_lines`
    tests in `test_git_controller.py`.

See `src/symmetria_ide/git_ops_controller.py`.
"""

from __future__ import annotations

import subprocess

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
