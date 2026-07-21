"""Bash-write attribution for the per-agent change filter (v2).

v1 tracks only write-tool (`tool_path`) provenance, so a Bash command that
writes files (`> f`, `sed -i`, `npm install`, a code generator, `make`) leaves
no `file_path` and never enters an agent's `touched` set — the "this agent"
side-panel scope silently misses it. This module closes that blind spot by
snapshot-diffing the agent's repo around each Bash command: the IDE probes the
git dirty set on the Bash `PreToolUse` edge and again on `PostToolUse`, and the
NEW dirty files (`post − pre`) are what that command wrote, attributed to the
agent that ran it.

`probe_dirty_leaves` shells out to git (WORKER-THREAD ONLY) and reuses
`git_controller.parse_porcelain_v2`; `bash_delta` is the pure set diff. The Qt
wiring (the probe pool, Pre/Post correlation on the slot record, folding the
delta into `touched`) lives in app.py's coordination section.

Timing characteristic (deliberate v2 trade-off): the reporter is
fire-and-forget, so the Pre probe races the command. For SLOW, high-value
writes (npm/generators/make — seconds long) the probe completes first and the
baseline is correct, so they are captured reliably. For ULTRA-fast writes
(`echo x > f`, sub-5ms) the baseline may already include the write and the file
is missed. The cases that motivated v2 are the slow ones.
"""

from __future__ import annotations

import os

from .git_controller import parse_porcelain_v2
from .git_subprocess import run_git

# git status is cheap; the bound only guards against a wedged git process
# holding a probe-pool worker. Matches git_subprocess._RESOLVE_TIMEOUT_SEC.
_STATUS_TIMEOUT_SEC = 5.0


def probe_dirty_leaves(root: str) -> set[str]:
    """Realpath'd absolute paths of every git-dirty leaf under `root`.

    Runs `git -C <root> status --porcelain=v2 -z` and reuses
    `parse_porcelain_v2` (which emits leaves only — the `·` dir aggregates are
    synthesized elsewhere, in GitController's scan). Empty set on any failure
    (not a repo, git missing, timeout) — a probe must never break attribution.

    WORKER-THREAD ONLY: it spawns a subprocess. Realpath'd so results compare
    equal to the `touched` set (which stores `os.path.realpath(tool_path)`) and
    to the fold's canonical matching in `_fold_agent_changes`.
    """
    if not root:
        return set()
    blob = run_git(root, "status", "--porcelain=v2", "-z", timeout=_STATUS_TIMEOUT_SEC)
    if not blob:
        return set()
    return {
        os.path.realpath(os.path.join(root, rel)) for rel in parse_porcelain_v2(blob)
    }


def bash_delta(pre: set[str], post: set[str]) -> set[str]:
    """Files that became dirty during a Bash command: `post − pre`.

    Pure. A file already dirty before the command (in `pre`) is NOT attributed
    — only what this command newly changed. A path that LEFT `post` (a deletion
    or a revert-to-HEAD) is intentionally dropped: the filter shows files that
    exist to inspect, and the touched∩dirty fold would exclude it anyway.
    """
    return post - pre
