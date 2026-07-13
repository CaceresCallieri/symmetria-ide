"""git_subprocess.GitExecutor — the shared local/remote git execution seam.

Pure/monkeypatched tests: the local path swaps subprocess.run, the remote
path injects a recording runner. This seam backs GitLogController,
GitBranchController, and GitOpsController in the VPS location (Phase 4 of
the location toggle); GitController carries its own equivalent internally.
"""

from __future__ import annotations

import subprocess

from symmetria_ide import git_subprocess
from symmetria_ide.git_subprocess import LOCAL_GIT, GitExecutor


def test_local_executor_runs_git_in_cwd(monkeypatch):
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(argv, 0, b"out", b"")

    monkeypatch.setattr(git_subprocess.subprocess, "run", fake_run)
    proc = LOCAL_GIT.execute("/repo", ["log", "-z"], timeout=5)
    assert proc is not None and proc.stdout == b"out"
    assert seen["argv"] == ["git", "log", "-z"]
    assert seen["cwd"] == "/repo"


def test_local_executor_degrades_to_none(monkeypatch):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("no git")

    monkeypatch.setattr(git_subprocess.subprocess, "run", fake_run)
    assert LOCAL_GIT.execute("/repo", ["log"], timeout=5) is None


def test_remote_executor_delegates_with_dash_c():
    calls: list[tuple] = []

    def runner(git_argv, timeout):
        calls.append((list(git_argv), timeout))
        return subprocess.CompletedProcess(git_argv, 0, b"remote-out", b"")

    executor = GitExecutor(
        remote_runner=runner,
        remote_root="/opt/dev/repos/demo",
        local_mount="/mnt/demo",
    )
    proc = executor.execute("/ignored-cwd", ["log", "-z"], timeout=7)
    assert proc is not None and proc.stdout == b"remote-out"
    assert calls == [(["git", "-C", "/opt/dev/repos/demo", "log", "-z"], 7)]


def test_remote_resolve_short_circuits_to_the_mount():
    executor = GitExecutor(
        remote_runner=lambda argv, timeout: None,
        remote_root="/opt/dev/repos/demo",
        local_mount="/mnt/demo",
    )
    # No runner call at all — the pairing probe already proved .git exists.
    assert executor.resolve_repo_root("/anything") == "/mnt/demo"


def test_run_git_returns_empty_bytes_on_failure_paths():
    failing = GitExecutor(
        remote_runner=lambda argv, timeout: None,  # transport failure
        remote_root="/r",
        local_mount="/m",
    )
    assert failing.run_git("/x", "log", timeout=5) == b""

    nonzero = GitExecutor(
        remote_runner=lambda argv, timeout: subprocess.CompletedProcess(
            argv, 128, b"", b"fatal: boom"
        ),
        remote_root="/r",
        local_mount="/m",
    )
    assert nonzero.run_git("/x", "log", timeout=5) == b""


def test_module_level_shorthands_are_the_local_executor(monkeypatch):
    seen: list = []
    monkeypatch.setattr(
        git_subprocess.subprocess,
        "run",
        lambda argv, **kw: (
            seen.append(argv) or subprocess.CompletedProcess(argv, 0, b"/repo\n", b"")
        ),
    )
    assert git_subprocess.resolve_repo_root("/somewhere") == "/repo"
    assert git_subprocess.run_git("/somewhere", "log", timeout=5) == b"/repo\n"
    assert all(argv[0] == "git" for argv in seen)


def test_executor_is_frozen_and_shareable():
    executor = GitExecutor()
    try:
        executor.remote_root = "/x"  # type: ignore[misc]
        raise AssertionError("GitExecutor must be frozen")
    except AttributeError:
        pass
