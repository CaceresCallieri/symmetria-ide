"""Shared subprocess helpers for the read-only git controllers.

``GitLogController`` and ``GitBranchController`` both resolve the repo root
and shell out to git from their worker threads with identical semantics
(capture output, bounded timeout, warn-and-degrade on failure). These
helpers are that one shared shape — a fix to the failure handling or
encoding propagates to every caller instead of drifting per controller.

``git_controller.py`` (the porcelain-status provider) deliberately keeps its
own subprocess machinery: its scan interleaves parsing with status-specific
error recovery and does not fit the plain run-and-return-stdout contract.

Worker-thread only, like the private methods these replace — nothing here
touches Qt.
"""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger(__name__)

_RESOLVE_TIMEOUT_SEC = 5.0


def resolve_repo_root(asked: str, *, timeout: float = _RESOLVE_TIMEOUT_SEC) -> str:
    """Run ``git rev-parse --show-toplevel``. Empty string = not a repo."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=asked,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        log.debug("git rev-parse failed for %s: %s", asked, exc)
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", errors="replace").strip()


def run_git(cwd: str, *args: str, timeout: float) -> bytes:
    """Run one git command, return raw stdout. ``b""`` on any failure.

    For commands where a zero exit code is the only success shape and the
    caller treats empty output as a benign degrade (log/for-each-ref/
    worktree-list). Commands with meaningful non-zero exits (``git diff
    --no-index`` returns 1 on difference) need their own handling and must
    not route through here.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
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
