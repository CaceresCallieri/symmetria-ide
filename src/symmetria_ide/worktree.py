"""Linked git-worktree detection — pure, synchronous, Qt-free.

Backs the "follow the focused agent's worktree" feature: when a terminal
agent's live cwd resolves into a LINKED worktree of the displayed project,
the IDE re-roots its chrome (tree + git surfaces) to that worktree and shows
a branch glyph on the agent's chip and the tree header (see AppController's
`_recompute_agent_root_override`).

Detection rides git's own on-disk contract, no subprocess: a linked
worktree's root holds a `.git` FILE (not dir) whose single line is

    gitdir: <main>/.git/worktrees/<name>

The `worktrees` path component is what distinguishes a worktree from a
SUBMODULE, whose `.git` file points at `<superproject>/.git/modules/<name>`
instead — a submodule checkout must NOT count as a worktree (its "main
root" is a different repository entirely).

Unit-testable like `agent_harness` / `project_browser_marker` — fixtures
are plain file writes, no git subprocess needed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def _gitdir_target(root: str) -> str:
    """The normalized `gitdir:` target of `root/.git`, or "" when `root/.git`
    is not a well-formed pointer FILE (missing, a real dir, unreadable, or
    malformed). Relative targets — git writes them in some configurations —
    are resolved against `root` itself, mirroring how git interprets them."""
    if not root:
        return ""
    dotgit = Path(root) / ".git"
    try:
        if not dotgit.is_file():
            return ""
        content = dotgit.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.debug("worktree: unreadable .git pointer at %s: %s", dotgit, e)
        return ""
    for line in content.splitlines():
        if line.startswith("gitdir:"):
            target = line[len("gitdir:") :].strip()
            if not target:
                return ""
            if not os.path.isabs(target):
                target = os.path.join(root, target)
            return os.path.normpath(target)
    return ""


def linked_worktree_info(root: str) -> tuple[str, str]:
    """`(main_root, worktree_name)` iff `root` is a linked git worktree,
    else `("", "")`.

    `root` should already be a repo root (e.g. from `resolve_project_root`).
    Valid only when the `.git` pointer file's target is
    `<main>/.git/worktrees/<name>` — the component check (`[-3] == ".git"`,
    `[-2] == "worktrees"`) is what rejects submodules (`.git/modules/...`)
    and arbitrary `gitdir:` redirections.
    """
    target = _gitdir_target(root)
    if not target:
        return "", ""
    parts = Path(target).parts
    if len(parts) < 4 or parts[-3] != ".git" or parts[-2] != "worktrees":
        return "", ""
    main_root = str(Path(*parts[:-3]))
    name = parts[-1]
    return main_root, name


def canonical_project_root(root: str) -> str:
    """The main-checkout root when `root` is a linked worktree; `root`
    unchanged otherwise. Stable under re-rooting: canonical(worktree) ==
    canonical(main checkout), which is what makes it the right comparator
    for "same repo?" checks and for registry publication."""
    main_root, _name = linked_worktree_info(root)
    return main_root or root
