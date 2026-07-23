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
from typing import TYPE_CHECKING

from .git_controller import parse_porcelain_v2
from .git_subprocess import LOCAL_GIT

if TYPE_CHECKING:
    from .git_controller import GitStatus

# git status is cheap; the bound only guards against a wedged git process
# holding a probe-pool worker. Matches git_subprocess._RESOLVE_TIMEOUT_SEC.
_STATUS_TIMEOUT_SEC = 5.0


def probe_dirty_leaves(root: str) -> set[str] | None:
    """Realpath'd absolute paths of every git-dirty leaf under `root`, or
    ``None`` on probe FAILURE (not a repo, git missing, timeout, lock loss).

    The ``None`` vs empty-set distinction is load-bearing: a failed Pre probe
    that returned an empty *set* would read as "clean baseline" and cause the
    Post probe's ENTIRE pre-existing dirty set to be attributed to one (possibly
    read-only) Bash command. Returning ``None`` lets the caller ABORT the
    attribution window instead of over-attributing; a genuinely CLEAN repo
    returns an empty set (success). Reuses `parse_porcelain_v2`, which emits
    leaves only — the `·` dir aggregates are synthesized elsewhere.

    Runs with ``core.optionalLocks=false`` (``GIT_OPTIONAL_LOCKS=0`` semantics)
    so the probe neither fails on, nor creates, ``.git/index.lock`` contention
    with the concurrent GitController scan / WorktreeWatcher on the same repo.

    WORKER-THREAD ONLY: it spawns a subprocess. Realpath'd so results compare
    equal to the `touched` set (which stores `os.path.realpath(tool_path)`) and
    to the fold's canonical matching in `_fold_agent_changes`.
    """
    if not root:
        return None
    proc = LOCAL_GIT.execute(
        root,
        ["-c", "core.optionalLocks=false", "status", "--porcelain=v2", "-z"],
        timeout=_STATUS_TIMEOUT_SEC,
    )
    if proc is None or proc.returncode != 0:
        return None  # failure — the caller aborts the attribution window
    return {
        os.path.realpath(os.path.join(root, rel))
        for rel in parse_porcelain_v2(proc.stdout)
    }


def probe_status_map(root: str) -> dict[str, GitStatus] | None:
    """Full git status map for ``root`` (repo-relative rel → :class:`GitStatus`),
    or ``None`` on probe FAILURE (not a repo, git missing, timeout, lock loss).

    The multi-root twin of :func:`probe_dirty_leaves`: same subprocess + parse,
    but returns the PARSED MAP (per-file status char + rename origin) instead of
    just the leaf set. The per-agent change filter's FOREIGN-repo sections need
    those chars to render M/A/?/D badges — the leaf set alone can't. The same
    ``None`` vs empty-map contract holds: ``None`` is a failure the caller must
    not treat as "clean" (a foreign section would vanish spuriously), whereas a
    genuinely clean repo returns an empty ``{}``.

    NOTE: line-count deltas (``additions``/``deletions``) are left at 0 — the
    numstat merge that populates them lives in ``GitController._do_scan`` and is
    displayed-repo-only, so foreign sections show badges without the ``+N -M``
    accessory. Acceptable for v1 (the badge is the signal; counts are polish).

    Runs with ``core.optionalLocks=false`` for the same lock-contention reason
    as :func:`probe_dirty_leaves`. WORKER-THREAD ONLY (spawns a subprocess).
    """
    if not root:
        return None
    proc = LOCAL_GIT.execute(
        root,
        ["-c", "core.optionalLocks=false", "status", "--porcelain=v2", "-z"],
        timeout=_STATUS_TIMEOUT_SEC,
    )
    if proc is None or proc.returncode != 0:
        return None  # failure — the caller keeps any prior snapshot, no wipe
    return parse_porcelain_v2(proc.stdout)


def bash_delta(pre: set[str], post: set[str]) -> set[str]:
    """Files that became dirty during a Bash command: `post − pre`.

    Pure. A file already dirty before the command (in `pre`) is NOT attributed
    — only what this command newly changed. A path that LEFT `post` (a deletion
    or a revert-to-HEAD) is intentionally dropped: the filter shows files that
    exist to inspect, and the touched∩dirty fold would exclude it anyway.
    """
    return post - pre
