"""Shared subprocess helpers for the read-only git controllers.

``GitLogController`` and ``GitBranchController`` both resolve the repo root
and shell out to git from their worker threads with identical semantics
(capture output, bounded timeout, warn-and-degrade on failure).
``GitOpsController`` shares the same execution seam for pull/push. The
``GitExecutor`` below is that one shared shape — a fix to the failure
handling or encoding propagates to every caller instead of drifting per
controller.

``GitExecutor`` also carries the VPS location's remote-execution seam: a
remote executor delegates every git invocation to an injected runner
(``git -C <remote_root> …`` over ssh — see ssh_runner.run_remote with
``text=False``) and short-circuits root resolution to the repo's SSHFS
mountpoint, so path-publishing callers keep emitting LOCAL paths. The
module-level ``resolve_repo_root`` / ``run_git`` functions are the local
executor's methods re-exported for callers that never go remote.

``git_controller.py`` (the porcelain-status provider) deliberately keeps its
own subprocess machinery: its scan interleaves parsing with status-specific
error recovery and does not fit the plain run-and-return-stdout contract
(it carries the same remote seam internally — see its ``set_remote``).

Worker-thread only, like the private methods these replace — nothing here
touches Qt.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

_RESOLVE_TIMEOUT_SEC = 5.0

# (git_argv, timeout) -> CompletedProcess with BYTES stdout/stderr, or None
# on transport failure. The VPS location injects ssh_runner.run_remote here.
RemoteRunner = Callable[[list[str], float], "subprocess.CompletedProcess | None"]


@dataclass(frozen=True)
class GitExecutor:
    """Where git subprocesses execute: locally (default) or remotely.

    Frozen + shared across worker threads without locks: controllers swap
    the whole object atomically (attribute assignment) and each request
    reads it once — a scan racing the swap uses either the old or the new
    executor wholesale, never a mix.
    """

    remote_runner: RemoteRunner | None = None
    remote_root: str = ""
    local_mount: str = ""

    @property
    def is_remote(self) -> bool:
        return self.remote_runner is not None

    def execute(
        self, cwd: str, args: list[str], *, timeout: float
    ) -> subprocess.CompletedProcess | None:
        """Raw run: CompletedProcess (bytes stdout/stderr) or None. Never raises."""
        if self.remote_runner is not None:
            return self.remote_runner(["git", "-C", self.remote_root, *args], timeout)
        try:
            return subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            log.debug("git %s failed for %s: %s", args[:1], cwd, exc)
            return None

    def resolve_repo_root(
        self, asked: str, *, timeout: float = _RESOLVE_TIMEOUT_SEC
    ) -> str:
        """``git rev-parse --show-toplevel``. Empty string = not a repo.

        Remote executors return the SSHFS mountpoint directly: the pairing
        probe already proved ``<remote_root>/.git`` exists, and the mount is
        the LOCAL identity every path-consumer must see.
        """
        if self.remote_runner is not None:
            return self.local_mount
        proc = self.execute(asked, ["rev-parse", "--show-toplevel"], timeout=timeout)
        if proc is None or proc.returncode != 0:
            return ""
        return proc.stdout.decode("utf-8", errors="replace").strip()

    def run_git(self, cwd: str, *args: str, timeout: float) -> bytes:
        """Run one git command, return raw stdout. ``b""`` on any failure.

        For commands where a zero exit code is the only success shape and the
        caller treats empty output as a benign degrade (log/for-each-ref/
        worktree-list). Commands with meaningful non-zero exits (``git diff
        --no-index`` returns 1 on difference) need their own handling via
        ``execute`` and must not route through here.
        """
        proc = self.execute(cwd, list(args), timeout=timeout)
        if proc is None:
            log.warning("git %s failed for %s", args[0], cwd)
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


LOCAL_GIT = GitExecutor()


def resolve_repo_root(asked: str, *, timeout: float = _RESOLVE_TIMEOUT_SEC) -> str:
    """Local-executor shorthand (callers that never go remote)."""
    return LOCAL_GIT.resolve_repo_root(asked, timeout=timeout)


def run_git(cwd: str, *args: str, timeout: float) -> bytes:
    """Local-executor shorthand (callers that never go remote)."""
    return LOCAL_GIT.run_git(cwd, *args, timeout=timeout)
