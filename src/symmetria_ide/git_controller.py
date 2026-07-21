"""Git status parsing + the GitController Qt facade.

Four layers, top-down:

  1. ``parse_porcelain_v2(blob: bytes) -> dict[str, GitStatus]`` — a pure
     parser for `git status --porcelain=v2 -z` stdout. No subprocess, no Qt,
     no I/O. Feed it bytes, get a path-keyed status map.

  2. ``_add_directory_aggregates(file_map) -> dict`` — a pure post-processor
     that synthesises aggregate entries for every ancestor directory of
     every changed path, so the file tree can render a badge on directories
     whose subtrees contain changes.

  3. Numstat layer — ``parse_numstat_blob``, ``_merge_numstat_into_map``,
     ``_compute_stats``: pure functions that parse ``git diff --numstat -z``
     output and fold additions/deletions into the status map and the
     ``GitStats`` header aggregate. No subprocess, no Qt, no I/O.

  4. ``GitController(QObject)`` — the Qt facade. Owns a worker thread that
     runs ``git rev-parse`` + ``git status`` + numstat passes on demand, a
     ``QFileSystemWatcher`` on ``.git/{index,HEAD,MERGE_HEAD,refs/heads/<branch>}``
     to trigger re-scans, and a 200ms debounce timer to coalesce bursts.
     Exposes ``statusForPath(absolute_path) -> QVariantMap`` and
     ``stats -> QVariantMap`` to QML and emits ``statusChanged`` /
     ``statsChanged`` once per scan.

Future phases will grow ``GitController`` with ``stage()`` / ``unstage()`` /
``commit()`` / ``branchList()`` slots — per the "same class, additive"
decision, the same object that owns the status map will own the operations
surface. This keeps the watcher and parsed map as a single source of truth.
"""

from __future__ import annotations

import gc
import logging
import os
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QFileSystemWatcher,
    QModelIndex,
    QObject,
    Qt,
    QTimer,
    Signal,
    Slot,
)

from .trace import trace
from .worktree_watcher import WorktreeWatcher

log = logging.getLogger(__name__)


# Semantic state names — map to FmTheme.gitStatus.* colors in QML.
# These are stable identifiers; the actual hex values live FM-side.
STATE_UNSTAGED = "unstaged"  # red    — worktree changes not yet staged
STATE_STAGED = "staged"  # green  — staged for next commit
STATE_UNTRACKED = "untracked"  # blue   — new file, never added to git
STATE_RENAMED = "renamed"  # orange — staged rename or copy
STATE_CONFLICTED = "conflicted"  # magenta — unmerged
STATE_IGNORED = "ignored"  # gray   — listed in .gitignore (rarely rendered)


@dataclass(slots=True, frozen=True)
class GitStatus:
    """One file's git status, normalized for badge rendering.

    `path` is repo-relative (porcelain v2 always emits paths relative to
    the repo root). `char` is the single character the badge displays.
    `state` selects the color via the FM's named-color palette.
    `orig_path` is populated only for rename/copy records (type `2`).
    `additions`/`deletions` carry the line-count delta from
    ``git diff (--cached)? --numstat`` for tracked files, or
    ``additions`` = file line count and ``deletions = 0`` for untracked.
    They default to 0 so `parse_porcelain_v2` can construct entries
    without knowing the diff yet — the merge step in `_do_scan`
    populates the fields afterward via ``dataclasses.replace``.
    """

    path: str
    char: str
    state: str
    tooltip: str
    orig_path: str | None = None
    additions: int = 0
    deletions: int = 0


@dataclass(slots=True, frozen=True)
class GitStats:
    """Aggregate line-change counts for the git status panel header.

    Three buckets mirroring the reference Claude statusline shape:
    staged (●), unstaged (○), untracked (✦). File counts come from the
    numstat output's path set, NOT from the rendered panel row count —
    a doubly-modified "MM" file legitimately appears in both the staged
    and unstaged buckets even though the panel renders only one row for
    it (worktree precedence per `_classify_xy`). The header is meant to
    answer "how much work do I have on each side", and that question is
    naturally split.

    Frozen so the cross-thread emit moves a single immutable value —
    QML reads it without holding a controller lock. Same discipline as
    `GitStatus`.
    """

    staged_add: int = 0
    staged_del: int = 0
    staged_files: int = 0
    unstaged_add: int = 0
    unstaged_del: int = 0
    unstaged_files: int = 0
    untracked_lines: int = 0
    untracked_count: int = 0


@dataclass(slots=True, frozen=True)
class GitBranchSync:
    """Ahead/behind commit counts of the current branch vs. its upstream.

    Populated from the ``# branch.ab +<ahead> -<behind>`` header that
    ``git status --porcelain=v2 --branch`` emits ONLY when the current branch
    has an upstream. Both default to 0, which is also the value when there is
    no upstream (a local-only branch), a detached HEAD, or an unborn branch —
    git omits the ``branch.ab`` line in all those states, so "no upstream" and
    "fully in sync" both render as no indicator. ``ahead`` is the count of
    local commits not yet on the remote (the "unpushed" number the status bar
    shows); ``behind`` is remote commits not yet pulled — only meaningful
    after a ``git fetch``, since git can't know about remote commits it hasn't
    seen. Frozen so the cross-thread publish moves one immutable value, same
    discipline as GitStats / GitStatus.
    """

    ahead: int = 0
    behind: int = 0


# Human-readable tooltips, indexed by (char, state).
# Unlisted combinations fall back to a generic "<state> (<char>)" at parse time
# so we never crash on a status code we haven't catalogued yet.
_TOOLTIPS: dict[tuple[str, str], str] = {
    ("M", STATE_UNSTAGED): "Modified",
    ("M", STATE_STAGED): "Modified (staged)",
    ("A", STATE_STAGED): "Added (staged)",
    ("D", STATE_UNSTAGED): "Deleted",
    ("D", STATE_STAGED): "Deleted (staged)",
    ("R", STATE_RENAMED): "Renamed",
    ("C", STATE_RENAMED): "Copied",
    ("T", STATE_UNSTAGED): "Type changed",
    ("T", STATE_STAGED): "Type changed (staged)",
    ("?", STATE_UNTRACKED): "Untracked",
    ("!", STATE_IGNORED): "Ignored",
    ("U", STATE_CONFLICTED): "Conflicted",
}


def _classify_xy(x: str, y: str) -> tuple[str, str]:
    """Reduce porcelain XY to a single (char, state).

    Worktree status (Y) takes precedence over index status (X) — the LazyGit
    convention. So an "MM" file (staged-then-modified) renders as unstaged
    red, not staged green: the user's most recent action is what they need
    to see first. The dual-badge "MM" rendering is a future seam, not v1.
    """
    if y != ".":
        return (y, STATE_UNSTAGED)
    if x != ".":
        return (x, STATE_STAGED)
    # Both dots means "no change" — porcelain doesn't emit these, but if a
    # future git version did, returning a sentinel makes the bug visible
    # rather than silently producing a misleading badge.
    return (".", STATE_UNSTAGED)


def _tooltip_for(char: str, state: str) -> str:
    return _TOOLTIPS.get((char, state), f"{state} ({char})")


def parse_porcelain_v2(blob: bytes) -> dict[str, GitStatus]:
    """Parse `git status --porcelain=v2 -z` stdout into a path→GitStatus map.

    Record formats (NUL-terminated when `-z` is in effect):

      ``# branch.<field> <value>``                                — header (skipped)
      ``1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>``            — ordinary
      ``2 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <X><score> <new>``  — rename/copy
        ↑ followed by a second NUL-terminated field carrying the original path.
      ``u <XY> <sub> <m1> <m2> <m3> <mW> <h1> <h2> <h3> <path>``  — unmerged
      ``? <path>``                                                — untracked
      ``! <path>``                                                — ignored

    Malformed records are silently skipped — partial output from a torn
    `git status` (process killed mid-write) must not crash the watcher loop.
    """
    result: dict[str, GitStatus] = {}
    if not blob:
        return result

    # NUL-split. Drop the trailing empty that always follows the final NUL.
    fields = blob.split(b"\x00")
    if fields and fields[-1] == b"":
        fields.pop()

    i = 0
    while i < len(fields):
        rec = fields[i].decode("utf-8", errors="replace")
        i += 1

        if not rec or rec.startswith("#"):
            continue

        if rec.startswith("1 "):
            parts = rec.split(" ", 8)
            if len(parts) < 9 or len(parts[1]) < 2:
                continue
            xy = parts[1]
            path = parts[8]
            char, state = _classify_xy(xy[0], xy[1])
            result[path] = GitStatus(
                path=path,
                char=char,
                state=state,
                tooltip=_tooltip_for(char, state),
            )

        elif rec.startswith("2 "):
            parts = rec.split(" ", 9)
            if len(parts) < 10 or len(parts[1]) < 2 or i >= len(fields):
                # Rename without its companion origPath field — malformed; skip
                # AND consume nothing further so the next iteration realigns.
                continue
            xy = parts[1]
            path = parts[9]
            orig_path = fields[i].decode("utf-8", errors="replace")
            i += 1
            char, state = _classify_xy(xy[0], xy[1])
            # Rename/copy use the same orange palette regardless of staging
            # side (porcelain only emits these as staged: X=R/C, Y=.).
            if char in ("R", "C"):
                state = STATE_RENAMED
            result[path] = GitStatus(
                path=path,
                char=char,
                state=state,
                tooltip=_tooltip_for(char, state),
                orig_path=orig_path,
            )

        elif rec.startswith("u "):
            parts = rec.split(" ", 10)
            if len(parts) < 11:
                continue
            path = parts[10]
            result[path] = GitStatus(
                path=path,
                char="U",
                state=STATE_CONFLICTED,
                tooltip=_tooltip_for("U", STATE_CONFLICTED),
            )

        elif rec.startswith("? "):
            path = rec[2:]
            result[path] = GitStatus(
                path=path,
                char="?",
                state=STATE_UNTRACKED,
                tooltip=_tooltip_for("?", STATE_UNTRACKED),
            )

        elif rec.startswith("! "):
            path = rec[2:]
            result[path] = GitStatus(
                path=path,
                char="!",
                state=STATE_IGNORED,
                tooltip=_tooltip_for("!", STATE_IGNORED),
            )

        # Anything else (unknown prefix from a future git version) is dropped
        # rather than crashing — favors forward compatibility over strictness.

    return result


def parse_branch_header(blob: bytes) -> tuple[GitBranchSync, str, str]:
    """Extract ``(ahead/behind sync, upstream ref, branch name)`` from
    porcelain v2 headers.

    Reads the leading ``# branch.*`` header block of
    ``git status --porcelain=v2 -z --branch`` output:

      ``# branch.head dev``               → branch name = ``"dev"``
        (``"(detached)"`` — git's literal — is normalized to ``""``)
      ``# branch.upstream origin/main``   → upstream = ``"origin/main"``
      ``# branch.ab +4 -1``               → ``GitBranchSync(ahead=4, behind=1)``

    With ``-z`` each header is its own NUL-terminated record (same framing the
    file records use), so we split on NUL exactly like ``parse_porcelain_v2``.
    Both lines are ABSENT when the branch has no upstream — the returned sync
    stays ``(0, 0)`` and upstream ``""``. The ``branch.ab`` count fields are
    signed (``+ahead`` / ``-behind``); ``int()`` parses the signs directly and
    we negate ``behind`` back to a positive count.

    The loop ITERATES only the header block: it breaks at the first non-header
    record (headers always lead the output), so it neither inspects the
    file-record tail nor over-scans, and relies on that break to terminate.
    ``blob.split`` does still materialize the whole record list up front — only
    the per-record work is short-circuited, not the allocation — but the blob
    is small relative to the status scan that produced it and
    ``parse_porcelain_v2`` splits it again anyway, so a header-only scan isn't
    worth the added complexity.
    """
    sync = GitBranchSync()
    upstream = ""
    head = ""
    if not blob:
        return (sync, upstream, head)
    for chunk in blob.split(b"\x00"):
        if not chunk:
            continue
        if not chunk.startswith(b"#"):
            break  # headers lead the output; first file record ends the block
        rec = chunk.decode("utf-8", errors="replace")
        if rec.startswith("# branch.head "):
            head = rec[len("# branch.head ") :].strip()
            if head == "(detached)":
                head = ""
        elif rec.startswith("# branch.upstream "):
            upstream = rec[len("# branch.upstream ") :].strip()
        elif rec.startswith("# branch.ab "):
            # rec == "# branch.ab +<ahead> -<behind>" → 4 whitespace tokens.
            parts = rec.split()
            if len(parts) >= 4:
                try:
                    ahead = int(parts[2])
                    behind = -int(parts[3])
                except ValueError:
                    ahead, behind = 0, 0
                # max(0, ...) guards against a malformed sign producing a
                # nonsensical negative count.
                sync = GitBranchSync(ahead=max(0, ahead), behind=max(0, behind))
    return (sync, upstream, head)


# ---------------------------------------------------------------------------
# Directory aggregation — synthesize ancestor entries for changed paths.
# ---------------------------------------------------------------------------

# State priority for picking the "dominant" colour of a directory aggregate.
# Higher value = more attention-worthy. Conflicts trump everything (the user
# MUST resolve them); unstaged trumps staged (the more recent action wins,
# same rationale as the worktree-precedence rule in `_classify_xy`).
_STATE_PRIORITY: dict[str, int] = {
    STATE_IGNORED: 0,
    STATE_UNTRACKED: 1,
    STATE_RENAMED: 2,
    STATE_STAGED: 3,
    STATE_UNSTAGED: 4,
    STATE_CONFLICTED: 5,
}


def _add_directory_aggregates(file_map: dict[str, GitStatus]) -> dict[str, GitStatus]:
    """Return a new map containing file entries PLUS one aggregate per ancestor.

    Each aggregate uses ``char='·'`` (middle dot) and the highest-priority
    state of its descendants. The tooltip is ``"N file(s) changed"`` for
    discoverability. Computed eagerly after every parse — cheap because N is
    typically tens, and depth rarely exceeds 10. Aggregating per call would
    be O(rows × depth) every paint frame; this pre-computation collapses it
    to O(1) hash lookup per ``statusForPath`` call (the contract the FM
    expects per its brief).
    """
    if not file_map:
        return {}

    result = dict(file_map)
    # ancestor path → (descendant count, dominant state)
    dir_info: dict[str, tuple[int, str]] = {}

    for path, status in file_map.items():
        parts = Path(path).parts
        # Walk every strict ancestor: "src/foo/bar.py" → "src/foo", "src".
        # `range(1, len(parts))` excludes the leaf, which is the file itself.
        for depth in range(1, len(parts)):
            ancestor = str(Path(*parts[:depth]))
            count, current_state = dir_info.get(ancestor, (0, STATE_IGNORED))
            count += 1
            # All states are keys in _STATE_PRIORITY; direct access
            # raises KeyError for unseen states, which is more useful
            # than silently treating them as priority -1.
            if _STATE_PRIORITY[status.state] > _STATE_PRIORITY[current_state]:
                current_state = status.state
            dir_info[ancestor] = (count, current_state)

    for dir_path, (count, state) in dir_info.items():
        plural = "" if count == 1 else "s"
        # If a file happens to share a path with an ancestor (impossible in
        # real git output, but defensively) the file entry wins — we don't
        # overwrite. The directory aggregate only fills paths NOT in file_map.
        if dir_path in file_map:
            continue
        result[dir_path] = GitStatus(
            path=dir_path,
            char="·",
            state=state,
            tooltip=f"{count} file{plural} changed",
        )

    return result


# ---------------------------------------------------------------------------
# numstat parsing + merge — pure functions that turn `git diff --numstat -z`
# output into a path-keyed (additions, deletions) dict, and fold those plus
# untracked line counts back into a GitStatus map / GitStats aggregate.
# ---------------------------------------------------------------------------


def parse_numstat_blob(blob: bytes) -> dict[str, tuple[int, int]]:
    """Parse ``git diff --numstat -z`` stdout into a path→(adds, dels) map.

    Wire format with `-z`:

      ``<adds>\\t<dels>\\t<path>\\x00``        — ordinary entry
      ``<adds>\\t<dels>\\t<orig>\\x00<new>\\x00`` — rename (two NUL fields)

    Binary files emit ``-`` for both numeric columns and are SKIPPED — the
    panel can't meaningfully render `+? -?` for a binary, and the bash
    reference behaves the same way (status-line.sh:77 / :89).

    For rename rows we key the result on the NEW path (the post-rename
    identity), so the merge step can match it against the porcelain v2
    type-2 record whose path field is also the new path. Original-path
    matching for renames isn't needed: the porcelain row already carries
    the rename pairing via `orig_path`, and the numstat row's delta is
    semantically attached to "the file as it now exists".

    Malformed records are silently dropped — partial pipe reads or future
    git output variants must not crash the watcher worker.
    """
    result: dict[str, tuple[int, int]] = {}
    if not blob:
        return result

    fields = blob.split(b"\x00")
    if fields and fields[-1] == b"":
        fields.pop()

    i = 0
    while i < len(fields):
        rec = fields[i].decode("utf-8", errors="replace")
        i += 1
        if not rec:
            continue
        # Split into max 3 cols: adds, dels, path-or-orig. Renames put the
        # new path in a SECOND NUL-terminated field that follows.
        parts = rec.split("\t", 2)
        if len(parts) < 3:
            continue
        adds_s, dels_s, path = parts
        # Binary rows: skip with no consumption beyond the row itself.
        if adds_s == "-" or dels_s == "-":
            if path == "" and i < len(fields):
                # Defensive: rename row with binary marker. Consume the
                # second field so the iterator realigns.
                i += 1
            continue
        try:
            adds = int(adds_s)
            dels = int(dels_s)
        except ValueError:
            continue
        # Rename detection: with `-z`, the FIRST field's path slot is empty
        # and the new path lives in the next NUL-terminated field.
        if path == "":
            if i >= len(fields):
                continue
            path = fields[i].decode("utf-8", errors="replace")
            i += 1
        result[path] = (adds, dels)

    return result


def _merge_numstat_into_map(
    file_map: dict[str, GitStatus],
    staged_ns: dict[str, tuple[int, int]],
    unstaged_ns: dict[str, tuple[int, int]],
    untracked_lc: dict[str, int],
) -> dict[str, GitStatus]:
    """Return a new map with additions/deletions populated on each GitStatus.

    Selection rule mirrors `_classify_xy`'s worktree-precedence:
      - state == unstaged  → unstaged_ns entry (worktree diff)
      - state == staged    → staged_ns entry (index diff)
      - state == untracked → (untracked_lc.get(path, 0), 0)
      - state == renamed/conflicted → staged_ns first, then unstaged_ns
        (rename rows live in the staged side; conflicts have entries in
        both depending on which side the user has touched — staged-first
        is the most useful default).
      - state == ignored   → no diff; leave 0/0.

    Files with no matching numstat entry (binary, rename-arrow path
    mismatch, etc.) keep the dataclass default of 0/0 — the row delegate
    hides its delta column when both are 0, so the failure is silent.

    `dataclasses.replace` is used because GitStatus is frozen.
    """
    result: dict[str, GitStatus] = {}
    for path, status in file_map.items():
        adds, dels = 0, 0
        if status.state == STATE_UNSTAGED:
            adds, dels = unstaged_ns.get(path, (0, 0))
        elif status.state == STATE_STAGED:
            adds, dels = staged_ns.get(path, (0, 0))
        elif status.state == STATE_UNTRACKED:
            adds = untracked_lc.get(path, 0)
        elif status.state in (STATE_RENAMED, STATE_CONFLICTED):
            if path in staged_ns:
                adds, dels = staged_ns[path]
            elif path in unstaged_ns:
                adds, dels = unstaged_ns[path]
        # STATE_IGNORED falls through with 0/0.
        if adds or dels:
            result[path] = replace(status, additions=adds, deletions=dels)
        else:
            result[path] = status
    return result


def _compute_stats(
    staged_ns: dict[str, tuple[int, int]],
    unstaged_ns: dict[str, tuple[int, int]],
    untracked_lc: dict[str, int],
    untracked_total: int,
) -> GitStats:
    """Sum numstat entries into the three header buckets.

    ``staged_files``/``unstaged_files`` count the path set of each numstat
    map. A doubly-modified "MM" file appears in BOTH dicts and is counted
    on both sides — that's intentional: it has real work on both the
    staging area and the worktree, and the user should see that.

    ``untracked_total`` is passed in separately so the count reflects the
    FULL untracked set even when ``untracked_lc`` is partial (cap hit).
    ``untracked_lines`` is the sum over whatever line counts we collected.
    """
    staged_add = sum(a for a, _ in staged_ns.values())
    staged_del = sum(d for _, d in staged_ns.values())
    unstaged_add = sum(a for a, _ in unstaged_ns.values())
    unstaged_del = sum(d for _, d in unstaged_ns.values())
    untracked_lines = sum(untracked_lc.values())
    return GitStats(
        staged_add=staged_add,
        staged_del=staged_del,
        staged_files=len(staged_ns),
        unstaged_add=unstaged_add,
        unstaged_del=unstaged_del,
        unstaged_files=len(unstaged_ns),
        untracked_lines=untracked_lines,
        untracked_count=untracked_total,
    )


# ---------------------------------------------------------------------------
# GitController — the Qt facade. Worker thread + watcher + debounce.
# ---------------------------------------------------------------------------

# Tunables. 200ms debounce is conservative — git index writes are fast and
# bursty (e.g., `git commit` rewrites index AND HEAD AND refs/heads/<branch>
# within milliseconds). Coalescing to one scan per burst is what the FM brief
# explicitly recommends.
_DEBOUNCE_MS = 200
# Remote-mode (VPS location) rescan cadence. inotify crosses neither SSHFS
# nor the network, so a poll timer replaces both watchers there. 5s balances
# freshness against one ssh-multiplexed git status per tick; editor saves and
# git ops still poke immediately through the same debounce funnel.
_REMOTE_POLL_MS = 5000
# Timeouts protect against pathological repos (e.g., a corrupted index, a
# `core.fsmonitor` daemon that hangs). The status scan can take seconds on
# very large monorepos; the rev-parse should be near-instant.
_RESOLVE_TIMEOUT_SEC = 5.0
_SCAN_TIMEOUT_SEC = 10.0
# Backstop for the not-a-repo → repo transition (canonically: a directory
# opened BEFORE `git init` ran in it). The directory SENTINEL watch (see
# `_arm_repo_sentinel`) is the primary, event-driven detector; this
# backed-off re-resolve covers the two cases the watch alone can miss —
# the sentinel watch failing to arm, and the `git init` partial-init race
# where `.git` exists but `rev-parse` momentarily fails AND subsequent
# writes land INSIDE `.git` (not as children of the watched root, so the
# directory watch won't re-fire). Backed-off so a genuinely-never-a-repo
# directory settles to ~one `git rev-parse` per minute while a directory
# that becomes a repo recovers within ~1-2s. Deliberately self-contained:
# no nvim capsule, no editor-save signal — only the filesystem — so this
# recovery path survives the eventual nvim deprecation untouched.
_SENTINEL_BACKSTOP_MIN_MS = 1000
_SENTINEL_BACKSTOP_MAX_MS = 60_000


def _fold_agent_changes(
    status_map: dict[str, GitStatus],
    resolved_root: str,
    keep_real: set[str],
) -> tuple[dict[str, bool], int]:
    """Pure projection behind :meth:`GitController.changed_path_set_for`.

    ``status_map`` is a repo-relative-path → :class:`GitStatus` map (the same
    shape ``parse_porcelain_v2`` produces, with ``·`` ancestor-dir aggregates
    synthesized by ``_add_directory_aggregates``). ``keep_real`` is the
    caller's target files, ALREADY realpath-normalized. Returns
    ``({absPath: True, ...}, leaf_count)`` covering every dirty LEAF (skipping
    ``·`` aggregates) that ``keep_real`` names, plus each such leaf's ancestor
    directories up to — and including — ``resolved_root``. That is exactly the
    membership shape ``FileTreeView.pathFilter`` needs to render those files
    and no others; ``leaf_count`` excludes the synthesized dirs.

    Iterates the (small) ``keep_real`` set, NOT the (potentially large) dirty
    tree: each touched file is mapped to its repo-relative key via
    ``os.path.relpath`` and looked up in ``status_map`` — O(touched) dict
    lookups with ZERO per-leaf ``realpath`` syscalls (the earlier forward form
    realpath'd every dirty leaf in the repo on the GUI thread, which janked on
    large changesets). ``resolved_root`` is realpath'd ONCE so the relpath
    lands on the same footing as the already-canonical ``keep_real`` entries —
    that is what lets a ``tool_path`` captured through a symlink still match
    git's canonical rel key. The OUTPUT paths join back onto the ORIGINAL
    ``resolved_root`` (what ``changedPathSet`` emits and the FM tree keys on),
    so a symlinked root stays consistent with the rest of the panel.

    No Qt, no lock: the caller snapshots ``_status_map``/``_resolved_root``
    and realpath-normalizes ``keep`` first, so this stays a unit-testable pure
    fold.
    """
    if not status_map or not resolved_root or not keep_real:
        return {}, 0
    root_real = os.path.realpath(resolved_root)
    out: dict[str, bool] = {resolved_root: True}
    count = 0
    for real_path in keep_real:
        rel = os.path.relpath(real_path, root_real)
        # Outside the repo (a foreign-repo edit) → skip. Bare `.`/`..` or a
        # `..`-prefixed rel all mean "not under root".
        if rel == os.curdir or rel == os.pardir or rel.startswith(os.pardir + os.sep):
            continue
        status = status_map.get(rel)
        if status is None or status.char == "·":
            continue  # the touched file is clean, or the key is a dir aggregate
        abs_leaf = os.path.join(resolved_root, rel)
        if abs_leaf in out:
            continue  # two touched paths resolved to the same leaf
        out[abs_leaf] = True
        count += 1
        # Walk up to the repo root, adding each ancestor dir so the FM tree
        # can expand down to the leaf. Stop at the root (already present) or
        # the first ancestor already in `out` (shared prefix of an earlier
        # leaf) — this bounds the walk to O(unique path segments).
        parent = os.path.dirname(abs_leaf)
        while parent and parent != resolved_root and parent not in out:
            out[parent] = True
            parent = os.path.dirname(parent)
    # No touched-and-dirty leaf → collapse the lone root entry to an empty map
    # so `focusedAgentChangesPathSet` reads as empty (drives the empty-state).
    return (out, count) if count else ({}, 0)


class GitController(QObject):
    """Async git-status provider + future git-operations surface.

    Threading model:
      - The owner thread (GUI thread, where QML lives) constructs this
        object, mutates ``repoRoot``, and reads ``statusForPath``.
      - A daemon worker thread runs the slow ops (``git rev-parse`` +
        ``git status``) so the GUI never blocks on git.
      - ``QFileSystemWatcher`` lives on the GUI thread (set as parent); its
        ``fileChanged`` slot starts a debounced ``QTimer`` whose timeout
        wakes the worker via a ``threading.Event``.
      - The worker emits ``statusChanged`` cross-thread; receivers in
        ``AppController`` and QML use ``Qt.QueuedConnection`` per project
        standards §4 P2.

    Lifecycle:
      - Construct on the GUI thread (typically from ``AppController.__init__``).
      - Bind ``repoRoot`` to the current project (e.g. via
        ``set_repo_root(project_path)`` driven by the
        ``StatusBarState.project`` capsule).
      - Call ``stop()`` at app teardown — sets ``_stop_event``, wakes the
        worker so it exits its wait, and joins the thread (≤1s).

    Project-standards anchors:
      §1 P0 — worker is ``daemon=True`` AND owns ``_stop_event``.
      §4 P2 — cross-thread signal connections use explicit ``Qt.QueuedConnection``
              with a grep-able comment at the connect site (responsibility of
              the receiver, e.g. AppController).
      gotcha #10 — GC is suspended around the cross-thread ``statusChanged``
              emit because the worker is constructing dicts under Python
              3.14's aggressive incremental GC.
    """

    statusChanged = Signal()
    statsChanged = Signal()
    # Fired (on the GUI thread, from `_publish`) whenever the ahead/behind
    # counts vs. the branch's upstream change. SEPARATE from `statusChanged`
    # because the two move independently: a `git push` zeroes `ahead` without
    # touching the working-tree file map, so a binding on `statusChanged`
    # alone would leave the status bar's `↑N` stale after a push.
    branchSyncChanged = Signal()
    repoRootChanged = Signal()
    # Fired (on the GUI thread) once per debounce window whenever the
    # working tree or `.git` state settles after a change — i.e. the same
    # coalesced moment a DEBOUNCED git re-scan is triggered. (The direct
    # `_wake_worker()` call in `_reset_state` deliberately does NOT emit
    # this: a repo-root reset implies no on-disk buffer change to reload.)
    # Distinct from `statusChanged` (which fires LATER, after the worker's
    # scan completes)
    # because consumers that only need "files on disk may have changed"
    # shouldn't wait on the git scan. AppController routes this to
    # `NvimBackend.checktime()` so open editor buffers reload when an agent
    # rewrites their files. See the wire in `AppController.__init__`.
    workingTreeChanged = Signal()
    # Internal worker→GUI signal that asks the GUI thread to rebuild the
    # QFileSystemWatcher entries for a newly-resolved repo root. Carries BOTH
    # the resolved root AND the upstream ref string (`origin/main`, "" when
    # none) so the GUI-side install works off a consistent snapshot of the
    # scan that queued it — reading the upstream live from shared state under
    # a rapid repo-switch could arm the watcher for root A with root B's
    # upstream. Connected with explicit QueuedConnection in __init__ — the
    # watcher is GUI-thread-owned and must never be touched from the worker.
    _watcherRefreshRequested = Signal(str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

        # `repoRoot` is the path the controller is ASKED to watch (typically
        # the project root from the nvim capsule). `_resolved_root` is what
        # `git rev-parse --show-toplevel` returned — they differ when the
        # asked-for path is a subdir of a repo, a worktree, or a submodule.
        # `statusForPath` does its relative-path conversion against
        # `_resolved_root`, not `_repo_root`.
        self._repo_root: str = ""
        self._resolved_root: str = ""
        self._status_map: dict[str, GitStatus] = {}
        # Header-bucket aggregates (staged/unstaged/untracked +a -d (n)).
        # Worker writes via `_publish`; GUI thread reads via the `stats`
        # property (which copies under the lock into a QVariantMap).
        self._stats: GitStats = GitStats()
        # Ahead/behind vs. upstream (porcelain `# branch.ab`). Worker writes in
        # `_publish`; the GUI thread reads via the `aheadCount`/`behindCount`
        # properties. Guarded by `_lock` like `_stats` / `_resolved_root`. The
        # raw upstream ref string (`# branch.upstream`) is NOT stored here — it
        # is needed only on the GUI thread to derive the remote ref file to
        # watch, so it rides the `_watcherRefreshRequested` signal as a
        # snapshot rather than living in shared state (see that signal's note).
        self._branch_sync: GitBranchSync = GitBranchSync()
        # Branch NAME from the same porcelain `# branch.head` header. The
        # status bar normally gets the name from the nvim capsule; the VPS
        # location has no nvim on the remote repo, so it binds this instead
        # ("" on detached/unborn HEAD). Rides branchSyncChanged.
        self._branch_name: str = ""
        # Remote-execution seam (VPS location). When set, every git
        # subprocess this controller runs is delegated to the runner (which
        # executes `git -C <remote_root> …` on the paired server over ssh)
        # and `_resolved_root` is pinned to `local_mount` (the repo's SSHFS
        # mountpoint) so every published absolute path stays a LOCAL path —
        # tree badges, Active Changes rows, and click-to-open all work
        # unchanged. Guarded by `_lock` (worker reads it per scan).
        self._remote_runner: (
            Callable[[list[str], float], subprocess.CompletedProcess | None] | None
        ) = None
        self._remote_root: str = ""
        self._remote_mount: str = ""
        # Absolute-path membership map of every gitignored file + directory
        # in the working tree. Computed via a single
        # ``git ls-files --others --ignored --exclude-standard --directory``
        # pass per status scan and exposed via the ``ignoredPathSet`` Qt
        # property. The FM ``FileTreeView`` consumes this through its
        # ``ignoredPathSet`` prop to short-circuit the per-directory
        # ``git check-ignore`` shell pipeline that ``Gitignore.qml`` would
        # otherwise spawn — which was the dominant cost driver for
        # auto-expand on medium-to-large repos (≥100 directories serialised
        # through one queue at ~30–40ms each).
        self._ignored_set: dict[str, bool] = {}
        # Tracer one-shot flag — flipped to True the first time `_publish`
        # emits a non-empty ignored set, so the trace lands on the moment
        # the FM's option-1 short-circuit data first becomes available.
        self._first_ignored_publish_traced = False

        # Guards `_status_map`, `_stats`, and `_resolved_root` against the
        # worker mutating them while the GUI thread reads via
        # `statusForPath` / the `stats` property.
        self._lock = threading.Lock()

        # Worker lifecycle.
        self._stop_event = threading.Event()
        self._scan_wakeup = threading.Event()
        self._worker = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="git-status-worker",
        )
        self._worker.start()

        # File watcher — lives on the GUI thread (parent=self). Re-armed on
        # every event because git's atomic-replace pattern unlinks the watched
        # inode (see `.git/index.lock → .git/index` rename).
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_watched_changed)
        # Directory-change wire: armed ONLY in the not-a-repo state, where
        # `_refresh_watcher_for_root` watches the project root itself as a
        # SENTINEL so that a `.git` appearing inside it (`git init`, clone,
        # the dir being replaced) wakes a re-scan that resolves and arms the
        # real watcher set. In the repo state no directories are watched
        # (only `.git` trigger FILES via `_install_watcher`), so this never
        # fires there. Self-contained recovery for a directory opened before
        # it became a repo — no nvim capsule, no editor save required.
        self._watcher.directoryChanged.connect(self._on_watched_dir_changed)

        # Debounce: bursts of file events (a single `git commit` touches
        # index, HEAD, MERGE_HEAD, refs/heads/<branch> in quick succession)
        # collapse to one scan.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._wake_worker)
        # Re-emit the debounce timeout as a public `workingTreeChanged`
        # edge. Signal-to-signal connection: every path that calls
        # `_debounce.start()` (worktree inotify events, the `gitpoke`
        # editor-save poke, AND `.git` trigger-file changes like a branch
        # switch) thus notifies external-reload consumers, coalesced to one
        # emit per window. No extra per-slot code — they all already funnel
        # through this one timer. Fires on the GUI thread (QTimer.timeout).
        self._debounce.timeout.connect(self.workingTreeChanged)

        # Not-a-repo → repo recovery backstop. See `_SENTINEL_BACKSTOP_*`
        # and `_arm_repo_sentinel`. Single-shot + manual re-arm so the
        # backoff delay can grow between attempts; `_sentinel_backstop_delay_ms`
        # carries the current delay. GUI-thread timer (parent=self).
        self._sentinel_backstop = QTimer(self)
        self._sentinel_backstop.setSingleShot(True)
        self._sentinel_backstop.timeout.connect(self._on_sentinel_backstop)
        self._sentinel_backstop_delay_ms = 0

        # Remote-mode refresh poll (VPS location): started by set_remote,
        # stopped by set_local. Fires into the debounce funnel so poll
        # ticks, manual pokes, and git-op refreshes all coalesce the same
        # way they do locally.
        self._remote_poll = QTimer(self)
        self._remote_poll.setInterval(_REMOTE_POLL_MS)
        self._remote_poll.timeout.connect(self._debounce.start)

        # Worker→GUI marshaling for watcher rebuild. Explicit QueuedConnection
        # is the project-standards §4 P2 pattern — worker emits, GUI slot runs.
        self._watcherRefreshRequested.connect(
            self._refresh_watcher_for_root,
            Qt.ConnectionType.QueuedConnection,
        )

        # Working-tree watcher — recursive watchdog/inotify observer over
        # the resolved repo root. Closes the gap the `.git` trigger-file
        # watcher leaves open: ordinary edits (editor saves, agent
        # rewrites, shell-pane appends, new untracked files) never touch
        # `.git`, so without this the status panel froze until the next
        # git operation. Events are pre-filtered on the observer thread
        # against `is_path_ignored` so ignored-tree churn (node_modules,
        # build outputs) never crosses into Qt. Re-pointed alongside the
        # `.git` watcher in `_refresh_watcher_for_root`.
        self._worktree_watcher = WorktreeWatcher(self.is_path_ignored, parent=self)
        # queued: WorktreeWatcher emitter thread → GUI debounce (§4 P2)
        self._worktree_watcher.changed.connect(
            self._on_worktree_changed,
            Qt.ConnectionType.QueuedConnection,
        )

    # -- QML-facing API ----------------------------------------------------

    @Property(str, notify=repoRootChanged)
    def repoRoot(self) -> str:
        return self._repo_root

    @Property("QVariant", notify=statsChanged)
    def stats(self) -> dict:
        """Header-bucket aggregates exposed to QML as a JS object.

        Marshalled as a `QVariantMap` so QML reads it as a plain object
        with the field names below. We copy under the lock so the worker
        can't swap the underlying `_stats` mid-read. Same idiom as
        `statusForPath` — small fixed-shape dict so QML can do
        `stats.stagedAdd` etc. without a roleNames dance.

        Field names match the QML-side use in `GitStatusPanel.qml`'s
        header repeater; renaming requires touching that file too.
        """
        with self._lock:
            s = self._stats
        return {
            "stagedAdd": s.staged_add,
            "stagedDel": s.staged_del,
            "stagedFiles": s.staged_files,
            "unstagedAdd": s.unstaged_add,
            "unstagedDel": s.unstaged_del,
            "unstagedFiles": s.unstaged_files,
            "untrackedLines": s.untracked_lines,
            "untrackedCount": s.untracked_count,
        }

    @Property(int, notify=branchSyncChanged)
    def aheadCount(self) -> int:
        """Local commits not yet pushed to the branch's upstream.

        0 when in sync, when there is no upstream, or on a detached/unborn
        HEAD — the status bar shows the `↑N` indicator only when this is > 0.
        """
        with self._lock:
            return self._branch_sync.ahead

    @Property(int, notify=branchSyncChanged)
    def behindCount(self) -> int:
        """Upstream commits not yet merged locally (meaningful post-`fetch`).

        Companion to `aheadCount`; the status bar renders `↓N` only when > 0.
        """
        with self._lock:
            return self._branch_sync.behind

    @Property(str, notify=branchSyncChanged)
    def branchName(self) -> str:
        """Current branch name from the porcelain `# branch.head` header.

        "" on detached/unborn HEAD or outside a repo. The status bar binds
        this in the VPS location (there is no nvim capsule for the remote
        repo); locally the capsule stays the branch-name source of truth.
        """
        with self._lock:
            return self._branch_name

    @Property("QVariant", notify=statusChanged)
    def ignoredPathSet(self) -> dict | None:
        """Absolute-path membership set of gitignored entries in the worktree.

        Returned as a ``{absPath: True}`` dict so the FM's QML can do an
        O(1) membership check inside its fan-out skip gate. Replaces the
        per-directory ``git check-ignore --stdin`` shell pipeline the FM
        otherwise spawns through ``Gitignore.qml``, which was strictly
        sequential (one subprocess at a time across the whole cascade)
        and dominated mount time on big repos.

        Paths are stripped of trailing slashes so they match the FM's
        ``FileSystemEntry.path`` semantics (also slash-free). Both files
        AND directories are included: an ignored directory (e.g.
        ``node_modules``) is listed via ``--directory`` aggregation so a
        single map entry covers the whole subtree without enumerating
        every child.

        Returns ``None`` (NOT an empty dict) when there's no resolved repo
        OR the first scan hasn't completed yet. The FM's QML gates its
        short-circuit on truthiness via ``if (root.ignoredPathSet)``, so a
        ``None`` return makes the FM fall back to its per-directory
        ``check-ignore`` path until real data arrives — crucial because a
        truthy-empty dict would cause the FM to treat NOTHING as ignored,
        over-expanding ``.venv`` / ``node_modules`` / etc. and exploding
        the cascade past the model ceiling before the GitController's
        first emit catches up.

        Notify on ``statusChanged`` — the ignored set is recomputed in the
        same worker pass that produces ``_status_map``, so reusing the
        existing emit keeps every binding consistent under one signal.
        """
        with self._lock:
            ignored, root = self._ignored_set, self._resolved_root
        if not root:
            return None
        # Empty repo (no ignored files) still publishes the empty map —
        # the consumer should treat empty-but-known as "skip nothing", not
        # as "data not available". Truthiness of `{}` is false in Python
        # but truthy in JS, so this contract reads cleanly on both sides.
        return dict(ignored)

    @Property("QVariant", notify=statusChanged)
    def changedPathSet(self) -> dict:
        """Absolute-path membership set for `FileTreeView.pathFilter`.

        Returns a dict shaped as ``{absPath: True, ...}`` covering
        rootPath itself plus every leaf file's absolute path plus every
        ancestor directory's absolute path. The ancestor entries are
        already present in ``_status_map`` (synthesized eagerly by
        ``_add_directory_aggregates``) — iterating the map is therefore
        a single-pass fold; no separate graph walk needed.

        Returns ``{}`` when there's no resolved repo OR the working tree
        is clean. Callers (the panel's auto-hide on ``model.count > 0``)
        normally suppress the empty case before it can render an empty
        embedded tree.

        Notify on ``statusChanged`` rather than a new signal — the
        membership set is fully derived from ``_status_map`` and changes
        exactly when the map does, so reusing the existing emit (already
        guarded by ``gc.disable`` per gotcha #10) keeps a single
        cross-thread emit window for everything the panel binds against.

        Reference-snapshotted: the lock captures a stable reference to
        the current ``_status_map`` dict. This is safe because
        ``_publish`` replaces ``_status_map`` with a new dict (never
        mutates it in place), so the captured reference remains valid
        after the lock is released. Contrast with ``_file_entries``,
        which copies via ``list(items())`` inside the lock — that
        method predates the replace-not-mutate invariant. The fold
        itself is O(N) over the map and runs on the GUI thread (QML
        binding evaluation context).
        """
        with self._lock:
            m, root = self._status_map, self._resolved_root
        if not m or not root:
            return {}
        root_path = Path(root)
        out: dict[str, bool] = {root: True}
        for rel in m:
            out[str(root_path / rel)] = True
        return out

    def changed_path_set_for(self, keep_abs: set[str]) -> tuple[dict, int]:
        """Filtered twin of :attr:`changedPathSet` scoped to a caller-supplied
        set of absolute file paths — the per-agent side-panel filter (v1).

        ``keep_abs`` is the set of files an agent has edited via a write-tool
        this session (its provenance). Returns ``({absPath: True}, file_count)``
        for the INTERSECTION of those files with the git working-tree changes,
        plus ancestor dirs + repo root — i.e. "the uncommitted changes this
        agent is responsible for". A touched file that matches HEAD is dropped;
        a dirty file the agent never touched is dropped. The heavy lifting is
        the pure :func:`_fold_agent_changes`; this method just snapshots the
        scan state under ``_lock`` and realpath-normalizes ``keep_abs`` once.

        Exists so the ``_resolved_root`` path projection lives in ONE place
        (AppController must not reach into ``_status_map``). Returns
        ``({}, 0)`` on no repo / clean tree / empty keep set.
        """
        if not keep_abs:
            return {}, 0
        with self._lock:
            m, root = self._status_map, self._resolved_root
        if not m or not root:
            return {}, 0
        # The sole caller (AppController._focused_agent_changes) already stores
        # realpath'd paths in `touched`, so this is idempotent belt-and-braces
        # — cheap for the small touched set, and keeps the method correct if a
        # future caller passes raw paths (the fold requires canonical input).
        keep_real = {os.path.realpath(p) for p in keep_abs}
        return _fold_agent_changes(m, root, keep_real)

    def set_repo_root(self, value: str) -> None:
        """Switch the repo being watched. Idempotent on equal values.

        Clears the existing status map immediately (synchronous, so the UI
        reflects the empty state without waiting for the worker) and asks
        the worker to re-resolve + re-scan against the new root.
        """
        if value == self._repo_root:
            return
        self._repo_root = value
        self.repoRootChanged.emit()
        # Tear down old watcher + map synchronously so callers don't see
        # stale data from the previous project for the brief window before
        # the worker produces the new scan. Clearing `_ignored_set` here
        # too is load-bearing for the FM `FileTreeView`'s gating contract:
        # the IDE-side Main.qml binds rootPath to a conditional that holds
        # at "" while `ignoredPathSet` is null. Leaving the previous
        # project's ignored set live across a switch would let the FM
        # mount the new tree with stale data — an ignored path from the
        # OLD project would either accidentally match (path collision) or
        # silently NOT match (treating nothing as ignored), producing
        # over-expansion of `.venv` / `node_modules` etc. before the new
        # scan catches up.
        self._clear_watcher()
        # A project switch resets the not-a-repo recovery backstop — the new
        # root's first scan re-arms it from the base delay if it, too, isn't
        # a repo yet.
        self._cancel_repo_sentinel_backstop()
        self._worktree_watcher.set_root("")
        self._reset_git_state()
        self._wake_worker()

    def set_remote(
        self,
        runner: Callable[[list[str], float], subprocess.CompletedProcess | None],
        remote_root: str,
        local_mount: str,
    ) -> None:
        """Point the controller at a REMOTE repo (VPS location).

        ``runner(git_argv, timeout)`` executes a full git argv on the paired
        server (ssh, worker-thread only) and returns a CompletedProcess with
        BYTES stdout, or None on transport failure. ``remote_root`` is the
        repo's path on the server (`git -C` target); ``local_mount`` is its
        SSHFS mountpoint here — it becomes `_resolved_root`, so the path
        translation is one assignment: porcelain's repo-relative paths join
        onto the mount and every consumer keeps working on local paths.

        Watchers stand down (inotify is blind across SSHFS/network) and the
        poll timer takes over. GUI-thread only, like set_repo_root.

        NB: this runner/remote_root/local_mount seam deliberately PARALLELS
        ``git_subprocess.GitExecutor`` (which the log/branch/ops controllers
        share) rather than consuming it — the scan pipeline's resolve
        short-circuit, watcher stand-down, and poll lifecycle are entangled
        with this class's locking. Unifying on GitExecutor is a known
        follow-up; until then, a change to remote git execution semantics
        must land in BOTH places.
        """
        self._clear_watcher()
        self._cancel_repo_sentinel_backstop()
        self._worktree_watcher.set_root("")
        with self._lock:
            self._remote_runner = runner
            self._remote_root = remote_root
            self._remote_mount = local_mount
        self._reset_git_state()
        self._remote_poll.start()
        self._wake_worker()

    def set_local(self) -> None:
        """Back to local execution (leave the VPS location). Idempotent."""
        with self._lock:
            was_remote = self._remote_runner is not None
            self._remote_runner = None
            self._remote_root = ""
            self._remote_mount = ""
        self._remote_poll.stop()
        if not was_remote:
            return
        self._reset_git_state()
        self._wake_worker()

    def _reset_git_state(self) -> None:
        """Clear all published scan state + notify (GUI thread).

        Shared by set_repo_root / set_remote / set_local — every root or
        location switch must empty the SAME field set synchronously so no
        consumer sees the previous context's data before the worker's fresh
        scan lands (see set_repo_root's ignored-set rationale).
        """
        with self._lock:
            self._status_map = {}
            self._stats = GitStats()
            self._resolved_root = ""
            self._ignored_set = {}
            self._branch_sync = GitBranchSync()
            self._branch_name = ""
        self.statusChanged.emit()
        self.statsChanged.emit()
        self.branchSyncChanged.emit()

    def _remote_spec(
        self,
    ) -> (
        tuple[
            Callable[[list[str], float], subprocess.CompletedProcess | None], str, str
        ]
        | None
    ):
        """(runner, remote_root, local_mount) snapshot, or None when local."""
        with self._lock:
            if self._remote_runner is None:
                return None
            return (self._remote_runner, self._remote_root, self._remote_mount)

    def _run_git(
        self, args: list[str], cwd: str, timeout: float
    ) -> subprocess.CompletedProcess | None:
        """The single git-subprocess choke point (worker thread).

        Local mode: today's ``subprocess.run(["git", *args], cwd=…)``.
        Remote mode: delegate the full argv to the injected runner, which
        executes ``git -C <remote_root> …`` on the paired server (cwd is
        irrelevant there). Returns None instead of raising on ANY failure —
        the call sites' error handling collapses to a None check with their
        site-specific log message.
        """
        remote = self._remote_spec()
        if remote is not None:
            runner, remote_root, _mount = remote
            return runner(["git", "-C", remote_root, *args], timeout)
        try:
            return subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            log.debug("git %s failed for %s: %s", args[0] if args else "?", cwd, exc)
            return None

    @Slot(str, result="QVariantMap")
    def statusForPath(self, absolute_path: str) -> dict:
        """Look up a path's git status by ABSOLUTE filesystem path.

        Returns an empty ``QVariantMap`` (``{}``) when:
          - we're not in a git repo (``_resolved_root`` is empty),
          - ``absolute_path`` is outside ``_resolved_root``,
          - or the path simply has no status (clean file).

        The FM's contract accepts ``null`` or an empty object identically —
        no badge is rendered. We return ``{}`` rather than ``None`` because
        ``QVariantMap`` marshaling treats ``None`` as a missing return and
        QML sees ``undefined``; ``{}`` is always a defined empty map.
        """
        with self._lock:
            resolved = self._resolved_root
            if not resolved:
                return {}
            try:
                rel = str(Path(absolute_path).relative_to(resolved))
            except ValueError:
                # Path is outside the repo — silent miss is correct (the FM
                # calls this for every visible row, including files in other
                # projects when switching).
                return {}
            status = self._status_map.get(rel)
        if status is None:
            return {}
        # `additions` / `deletions` flow through to the adapter in
        # Main.qml, which forwards them as `adds` / `dels` on the
        # statusProvider payload. The embedded FileTreeView in
        # `GitStatusPanel` renders those as a `+N -M` accessory next to
        # the badge. Always included (default 0 means no accessory) so
        # the QML side never has to disambiguate "field absent" from
        # "zero counts". Mirrors the role payload used by
        # `GitStatusListModel` so both consumption paths stay in sync.
        result: dict[str, object] = {
            "char": status.char,
            "state": status.state,
            "tooltip": status.tooltip,
            # Resolved-root-relative path (porcelain v2 emits paths relative
            # to the repo TOPLEVEL, not the asked `repoRoot` — which may be a
            # subdir/worktree/submodule). Identical to GitStatusListModel's
            # `displayName` role. The git viewer's working-diff request keys
            # on this, and `git diff` runs with cwd=_resolved_root, so the
            # path MUST be resolved-relative — deriving it by stripping the
            # asked `repoRoot` in QML breaks on the asked≠resolved case.
            "path": status.path,
            "additions": status.additions,
            "deletions": status.deletions,
        }
        if status.orig_path is not None:
            result["origPath"] = status.orig_path
        return result

    def _file_entries(self) -> list[tuple[str, GitStatus]]:
        """Return all non-aggregate entries as ``(absolute_path, status)`` pairs.

        Directory aggregates (``char == '·'``) are filtered out — they belong
        in the file tree's badges, not in the changes panel which is meant to
        be a flat list of *files* the user has touched. Sorted alphabetically
        by repo-relative path so the panel has a stable display order across
        scans (no jitter on bursty re-parses).

        Returns an empty list when ``_resolved_root`` is unset.
        """
        with self._lock:
            resolved = self._resolved_root
            snapshot = list(self._status_map.items())
        if not resolved:
            return []
        out: list[tuple[str, GitStatus]] = []
        for rel, status in snapshot:
            if status.char == "·":
                continue
            out.append((os.path.join(resolved, rel), status))
        out.sort(key=lambda pair: pair[1].path)
        return out

    # -- Lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        """Signal the worker to exit and join it (≤1s).

        Called from ``AppController.shutdown`` so the daemon thread doesn't
        outlive the Qt event loop. Setting both events is what makes the
        worker exit the ``_scan_wakeup.wait()`` blocking call — otherwise
        the thread would sit in the wait forever (daemon threads survive
        but a clean join is the project standard).
        """
        self._stop_event.set()
        self._scan_wakeup.set()
        self._worker.join(timeout=1.0)
        self._worktree_watcher.stop()
        # GUI-thread timers; stand them down so a pending fire can't, after
        # shutdown has begun, wake the joined scan worker OR — via the
        # `workingTreeChanged` → GitLogController.reload wire — queue a `git log`
        # into the still-alive history worker between this stop() and the log
        # controller's. Called on the GUI thread (shutdown).
        self._debounce.stop()
        self._sentinel_backstop.stop()
        self._remote_poll.stop()

    # -- Worker thread -----------------------------------------------------

    def _run_loop(self) -> None:
        """Worker thread main: wait → scan → wait. Never raises."""
        while not self._stop_event.is_set():
            self._scan_wakeup.wait()
            if self._stop_event.is_set():
                return
            self._scan_wakeup.clear()
            try:
                self._do_scan()
            except Exception:  # noqa: BLE001
                # The worker must NEVER crash — the daemon thread dying
                # silently would leave the watcher firing into a void.
                log.exception("git status scan failed")

    def _do_scan(self) -> None:
        """One scan: resolve repo root, run status + numstat, update map.

        Sequence:
          1. `git rev-parse --show-toplevel` — resolve real repo root.
          2. `git status --porcelain=v2 -z` — file-state map.
          3. `git diff --cached --numstat -z` — staged additions/deletions.
          4. `git diff --numstat -z` — unstaged additions/deletions.
          5. Python file-read pass over untracked files (cap 20).
          6. Merge into status map; compute header stats; publish.

        Steps 3-5 are best-effort: each falls back to an empty dict on
        subprocess failure, so a transient git-shell hiccup degrades to
        "no line counts shown" rather than blinking the whole panel away.
        The status scan (step 2) keeps its preserve-previous-on-None
        contract because it drives the file LIST, not just the counts.
        """
        with self._lock:
            repo_root = self._repo_root
        if not repo_root:
            self._publish({}, "", GitStats(), {}, GitBranchSync(), "")
            return

        remote = self._remote_spec()
        if remote is not None:
            # Remote mode: the mountpoint IS the resolved root (the pairing
            # probe already proved `<remote_root>/.git` exists — no
            # rev-parse round-trip), which is the whole path-translation
            # trick: porcelain rel-paths join onto the mount and every
            # published absolute path stays local.
            _runner, _remote_root, resolved = remote
        else:
            resolved = self._resolve_repo_root(repo_root)
        if not resolved:
            # Not a git repo — clear map, emit (panel hides), and tear the
            # watchers down (empty root clears both the .git trigger files
            # and the working-tree observer). Matters when a watched repo
            # STOPS being one mid-session (`.git` deleted) — without the
            # emit, the working-tree observer would keep firing into a
            # permanently-empty scan loop.
            self._publish({}, "", GitStats(), {}, GitBranchSync(), "")
            self._watcherRefreshRequested.emit("", "")
            return

        new_map, branch_sync, upstream, branch_name = self._run_status(resolved)
        if new_map is None:
            # Scan failed; preserve previous map rather than blink to empty
            # on a transient error (timeout, fsmonitor hiccup). The branch
            # sync is intentionally NOT published here — a transient git
            # failure shouldn't zero a valid `↑N`; the previous value holds.
            return

        staged_ns = self._run_numstat_staged(resolved) or {}
        unstaged_ns = self._run_numstat_unstaged(resolved) or {}
        untracked_paths = [
            rel for rel, st in new_map.items() if st.state == STATE_UNTRACKED
        ]
        untracked_lc = self._count_untracked_lines(resolved, untracked_paths)

        new_map = _merge_numstat_into_map(new_map, staged_ns, unstaged_ns, untracked_lc)
        new_stats = _compute_stats(
            staged_ns, unstaged_ns, untracked_lc, len(untracked_paths)
        )

        new_map = _add_directory_aggregates(new_map)
        # Ignored-path set published alongside the status map so QML
        # consumers see them together under one `statusChanged` emit.
        # Best-effort: on subprocess failure we publish an empty set
        # (the FM falls back to its per-dir check-ignore path).
        new_ignored = self._run_ignored_set(resolved)
        self._publish(
            new_map, resolved, new_stats, new_ignored, branch_sync, branch_name
        )

        if remote is not None:
            # Remote mode: NO watcher rebuild — inotify sees neither the
            # SSHFS mount nor the server's .git (set_remote already cleared
            # both watcher sets; the poll timer owns the refresh cadence).
            # Emitting ("", "") here instead would arm the not-a-repo
            # SENTINEL on the local project root — pointless churn.
            return
        # Hand the watcher rebuild back to the GUI thread — QFileSystemWatcher
        # is not thread-safe and lives on `self`'s thread. `upstream` rides
        # along so `_install_watcher` arms the remote-ref watch off the same
        # scan's snapshot rather than a live shared-state read.
        self._watcherRefreshRequested.emit(resolved, upstream)

    def _resolve_repo_root(self, asked: str) -> str:
        """Run ``git rev-parse --show-toplevel`` and return the real root.

        Empty string means "not a git repo" — callers branch on truthiness.
        """
        proc = self._run_git(
            ["rev-parse", "--show-toplevel"], asked, _RESOLVE_TIMEOUT_SEC
        )
        if proc is None or proc.returncode != 0:
            return ""
        return proc.stdout.decode("utf-8", errors="replace").strip()

    def _run_status(
        self, cwd: str
    ) -> tuple[dict[str, GitStatus] | None, GitBranchSync, str, str]:
        """Run ``git status --porcelain=v2 -z --branch`` and parse the output.

        Returns ``(file_map, branch_sync, upstream_ref, branch_name)``.
        ``file_map`` is ``None`` on subprocess failure (the caller preserves
        the previous map) and an empty dict on a clean tree. The other three
        come from the same single ``git status`` invocation's ``--branch``
        headers — no extra subprocess — and fall back to empty defaults on
        failure so the ahead/behind indicator clears rather than freezes on
        the last good value.
        """
        proc = self._run_git(
            ["status", "--porcelain=v2", "-z", "--branch", "--untracked-files=all"],
            cwd,
            _SCAN_TIMEOUT_SEC,
        )
        if proc is None:
            log.warning("git status failed for %s", cwd)
            return (None, GitBranchSync(), "", "")
        if proc.returncode != 0:
            log.warning(
                "git status exited %d for %s: %s",
                proc.returncode,
                cwd,
                proc.stderr.decode("utf-8", errors="replace").strip(),
            )
            return (None, GitBranchSync(), "", "")
        branch_sync, upstream, head = parse_branch_header(proc.stdout)
        return (parse_porcelain_v2(proc.stdout), branch_sync, upstream, head)

    def _run_numstat_staged(self, cwd: str) -> dict[str, tuple[int, int]] | None:
        """Run ``git diff --cached --numstat -z`` and parse the output.

        Captures additions/deletions for files staged in the index. Returns
        an empty dict when nothing is staged, ``None`` on subprocess failure
        — callers fold ``None`` to ``{}`` so a transient error degrades to
        "no staged line counts" rather than blanking the panel.
        """
        return self._run_numstat(cwd, cached=True)

    def _run_numstat_unstaged(self, cwd: str) -> dict[str, tuple[int, int]] | None:
        """Run ``git diff --numstat -z`` and parse the output.

        Captures additions/deletions for worktree changes (not yet staged).
        Same failure semantics as :meth:`_run_numstat_staged`.
        """
        return self._run_numstat(cwd, cached=False)

    def _run_numstat(
        self, cwd: str, *, cached: bool
    ) -> dict[str, tuple[int, int]] | None:
        """Shared subprocess shell for staged / unstaged numstat passes.

        Splitting the public-named methods keeps the call sites in
        ``_do_scan`` self-documenting; the shared body just toggles the
        ``--cached`` flag. ``-z`` makes path delimiters NUL so we can
        round-trip filenames containing tabs / newlines safely.
        """
        args = ["diff", "--cached" if cached else None, "--numstat", "-z"]
        proc = self._run_git([a for a in args if a is not None], cwd, _SCAN_TIMEOUT_SEC)
        if proc is None:
            log.warning("git diff --numstat (cached=%s) failed for %s", cached, cwd)
            return None
        if proc.returncode != 0:
            log.warning(
                "git diff --numstat (cached=%s) exited %d for %s: %s",
                cached,
                proc.returncode,
                cwd,
                proc.stderr.decode("utf-8", errors="replace").strip(),
            )
            return None
        return parse_numstat_blob(proc.stdout)

    def _count_untracked_lines(
        self,
        resolved: str,
        rel_paths: list[str],
        cap: int = 20,
    ) -> dict[str, int]:
        """Count newlines in each untracked file (binary heuristic + cap).

        Pure-Python file I/O, not ``wc -l``: spawning 20 subprocesses per
        debounced scan would add ~10ms+ of fork+exec on this system, well
        inside what users feel as a sluggish refresh. Python's ``open +
        read + count`` runs at ~50k files/s on a modern SSD.

        Cap: when more than ``cap`` paths are untracked (e.g. fresh clone
        of a repo with thousands of build artifacts), we sample only the
        first ``cap``. The header still shows the FULL untracked file
        count via ``untracked_count`` (computed separately upstream); only
        the line total reflects the partial sample. This mirrors the bash
        reference's responsiveness budget (status-line.sh:103).

        Binary heuristic: skip files whose first 8 KiB contain a NUL byte,
        matching how ``git diff --numstat`` itself classifies binaries.
        Empty files count as 0 newlines (correct).

        Returns repo-relative-path → line-count. Missing / unreadable
        files are silently dropped — best-effort by contract.
        """
        result: dict[str, int] = {}
        if not rel_paths:
            return result
        sample = rel_paths[:cap]
        for rel in sample:
            abs_path = os.path.join(resolved, rel)
            try:
                with open(abs_path, "rb") as f:
                    head = f.read(8192)
                    if b"\x00" in head:
                        continue  # binary file — skip per `git diff` semantics
                    rest = f.read()
            except OSError:
                continue
            result[rel] = head.count(b"\n") + rest.count(b"\n")
        return result

    def _run_ignored_set(self, cwd: str) -> dict[str, bool]:
        """Run ``git ls-files --others --ignored --exclude-standard --directory``
        and return absolute paths of every ignored entry in the worktree.

        ``--directory`` collapses entirely-ignored subtrees into a single
        directory entry (e.g. ``node_modules/`` rather than every file
        inside), keeping the result set small even on heavy repos like
        Node projects. Returned paths are absolute and slash-stripped so
        membership checks match the FM's ``FileSystemEntry.path``
        semantics. On subprocess failure we return ``{}`` — the FM falls
        through to its own per-directory ``check-ignore`` path, which is
        slower but functionally equivalent.
        """
        proc = self._run_git(
            [
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--directory",
                "-z",
            ],
            cwd,
            _SCAN_TIMEOUT_SEC,
        )
        if proc is None:
            log.warning("git ls-files (ignored) failed for %s", cwd)
            return {}
        if proc.returncode != 0:
            log.warning(
                "git ls-files (ignored) exited %d for %s: %s",
                proc.returncode,
                cwd,
                proc.stderr.decode("utf-8", errors="replace").strip(),
            )
            return {}
        out: dict[str, bool] = {}
        # -z gives NUL-delimited paths so newlines in filenames are safe.
        for chunk in proc.stdout.split(b"\x00"):
            if not chunk:
                continue
            rel = chunk.decode("utf-8", errors="replace")
            # `--directory` includes a trailing slash on dir entries; the
            # FM's entry.path has no trailing slash, so strip it for
            # consistent membership comparison.
            if rel.endswith("/"):
                rel = rel[:-1]
            out[os.path.join(cwd, rel)] = True
        return out

    def _publish(
        self,
        new_map: dict[str, GitStatus],
        resolved: str,
        new_stats: GitStats,
        new_ignored: dict[str, bool],
        new_sync: GitBranchSync,
        new_branch_name: str = "",
    ) -> None:
        """Swap the map + stats under the lock and emit change signals.

        GC is suspended around the emits because the worker thread is mid-
        cross-thread signal dispatch — same hazard as nvim_backend's
        ``_dispatch_redraw`` per gotcha #10 (Python 3.14 cyclic GC racing
        the Qt receiver-side wrapper allocation). One ``gc.disable`` window
        covers all signals so the protection is unbroken across them.
        """
        with self._lock:
            old_map = self._status_map
            old_stats = self._stats
            old_root = self._resolved_root
            old_ignored = self._ignored_set
            old_sync = self._branch_sync
            old_branch_name = self._branch_name
            self._status_map = new_map
            self._stats = new_stats
            self._resolved_root = resolved
            self._ignored_set = new_ignored
            self._branch_sync = new_sync
            self._branch_name = new_branch_name

        map_or_root_changed = (
            old_map != new_map or old_root != resolved or old_ignored != new_ignored
        )
        stats_changed = old_stats != new_stats
        # Branch NAME rides the same notify as ahead/behind — both are
        # `# branch.*` header facts and share every consumer's refresh point.
        sync_changed = old_sync != new_sync or old_branch_name != new_branch_name

        if map_or_root_changed:
            # Counting non-aggregate entries for the log line — directory
            # aggregates are an implementation detail, the user cares about
            # how many actual files have changes.
            file_count = sum(1 for s in new_map.values() if s.char != "·")
            if resolved:
                log.info(
                    "git scan: %d file(s) changed in %s",
                    file_count,
                    resolved,
                )
            else:
                log.info("git scan: cleared (not in a repo)")

        if map_or_root_changed or stats_changed or sync_changed:
            gc.disable()
            try:
                if map_or_root_changed:
                    self.statusChanged.emit()
                if stats_changed:
                    self.statsChanged.emit()
                if sync_changed:
                    self.branchSyncChanged.emit()
            finally:
                gc.enable()

        if not self._first_ignored_publish_traced and new_ignored:
            self._first_ignored_publish_traced = True
            trace("git_ignored_published")

    # -- GUI-thread slots --------------------------------------------------

    @Slot(str)
    def _refresh_watcher_for_root(self, resolved: str, upstream: str) -> None:
        """Install QFileSystemWatcher entries for the given repo root.

        Called via QueuedConnection from the worker's `_watcherRefreshRequested`
        emit, so runs on the GUI thread where `_watcher` lives. `upstream` is
        the scan's snapshot of the branch's upstream ref (`origin/main`, "" when
        none), passed through so the remote-ref watch is armed off a consistent
        snapshot rather than a live shared-state read (see the signal's note).
        """
        self._clear_watcher()
        # Re-point the recursive working-tree observer at the same moment
        # the .git trigger files re-arm — both watchers track the RESOLVED
        # root, not the asked-for path. set_root is idempotent on equal
        # values, so the per-scan call is free in the steady state.
        self._worktree_watcher.set_root(resolved)
        if not resolved:
            # Not a git repo (yet). Arm the directory SENTINEL on the asked
            # root so a `.git` appearing inside it recovers us, plus the
            # backed-off re-resolve backstop. This is what lets a directory
            # opened BEFORE `git init` ever show real status: without it the
            # one-shot resolve found no repo and NOTHING re-checked, freezing
            # the panel "clean" for the life of the instance even as the repo
            # filled with commits (the bug this whole path fixes).
            self._arm_repo_sentinel()
            return
        # A repo resolved — the recovery machinery has done its job; stand the
        # backstop down so it stops re-resolving.
        self._cancel_repo_sentinel_backstop()
        git_dir = self._git_dir_for(resolved)
        if git_dir:
            self._install_watcher(git_dir, upstream)

    def _arm_repo_sentinel(self) -> None:
        """Watch the asked-for root dir while it is NOT yet a repo.

        A single QFileSystemWatcher directory entry on `_repo_root` —
        `directoryChanged` fires when a child (notably `.git`) is created,
        routing through `_on_watched_dir_changed`. Pairs with the backed-off
        re-resolve backstop for the cases the watch alone can miss (arm
        failure, partial-init race). GUI-thread only (reached from
        `_refresh_watcher_for_root`, a queued slot).

        No-op on the directory watch when `_repo_root` is not a real directory
        (e.g. a path since deleted) — the backstop still runs so recovery isn't
        lost if it reappears. The empty-`_repo_root` branch here is defensive
        only: `_refresh_watcher_for_root("")` is reached solely with `_repo_root`
        SET (a resolve that failed on a real asked path), because `_do_scan`
        early-returns for an empty `_repo_root` without ever arming the sentinel.
        """
        root = self._repo_root
        if root and os.path.isdir(root):
            if not self._watcher.addPath(root):
                # Arm failure (inotify watch limit, transient inaccessibility).
                # The backstop covers recovery, but leave a breadcrumb: the
                # opened-before-init bug was first diagnosed via /proc inotify
                # inspection, and a missing sentinel watch is the tell.
                log.debug("git sentinel: failed to watch %s; backstop will cover", root)
        self._ensure_repo_sentinel_backstop()

    def _ensure_repo_sentinel_backstop(self) -> None:
        """Ensure the not-a-repo re-resolve backstop is counting down.

        Idempotent: if already active (possibly at a backed-off delay), leave
        it — re-arming on every not-a-repo re-scan would pin the delay at the
        base value and defeat the backoff. Kicks it off from cold at the base
        delay. GUI-thread only.
        """
        if self._sentinel_backstop.isActive():
            return
        self._sentinel_backstop_delay_ms = _SENTINEL_BACKSTOP_MIN_MS
        self._sentinel_backstop.start(self._sentinel_backstop_delay_ms)

    def _cancel_repo_sentinel_backstop(self) -> None:
        """Stop the backstop — a repo resolved or the root switched."""
        self._sentinel_backstop.stop()
        self._sentinel_backstop_delay_ms = 0

    @staticmethod
    def _git_dir_for(repo_root: str) -> str:
        """Return the .git directory path for a repo root.

        For ordinary repos this is ``<repo_root>/.git``. For worktrees and
        submodules the ``.git`` entry is a FILE containing ``gitdir: <path>``
        — we follow it to find the real git dir. Returns empty string when
        nothing usable is found.
        """
        candidate = os.path.join(repo_root, ".git")
        if os.path.isdir(candidate):
            return candidate
        if os.path.isfile(candidate):
            try:
                with open(candidate, encoding="utf-8") as f:
                    content = f.read().strip()
            except OSError:
                return ""
            if content.startswith("gitdir: "):
                gitdir = content[len("gitdir: ") :]
                if not os.path.isabs(gitdir):
                    gitdir = os.path.join(repo_root, gitdir)
                if os.path.isdir(gitdir):
                    return gitdir
        return ""

    def _install_watcher(self, git_dir: str, upstream: str) -> None:
        """Add the .git trigger files to the watcher, if they exist.

        ``upstream`` is the branch's upstream ref (`origin/main`, "" when none),
        snapshotted from the scan that queued this refresh — used to derive the
        remote-tracking ref file to watch.
        """
        candidates = [
            os.path.join(git_dir, "index"),
            os.path.join(git_dir, "HEAD"),
            os.path.join(git_dir, "MERGE_HEAD"),
        ]
        # Also watch the current branch's ref file so commits to the current
        # branch (which rewrite refs/heads/<branch>) trigger a scan.
        ref_path = self._current_branch_ref(git_dir)
        if ref_path:
            candidates.append(ref_path)

        # Watch the upstream remote-tracking ref + packed-refs so a `git push`
        # refreshes the ahead/behind counts. A push updates
        # refs/remotes/<remote>/<branch> (loose) OR rewrites packed-refs
        # (packed) — NEITHER is among the files above, and WorktreeWatcher
        # explicitly skips `.git`, so without these the status bar's `↑N`
        # would stay stale until the next unrelated scan trigger (a commit,
        # save, or branch switch). The loose ref may be absent when refs are
        # packed (the existence filter drops it); packed-refs is the companion
        # trigger that covers the packed case. Re-derived every refresh, so a
        # branch's upstream becoming set on a later scan gets picked up.
        remote_ref = self._upstream_ref_path(git_dir, upstream)
        if remote_ref:
            candidates.append(remote_ref)
        candidates.append(os.path.join(git_dir, "packed-refs"))

        existing = [p for p in candidates if os.path.exists(p)]
        if existing:
            self._watcher.addPaths(existing)

    @staticmethod
    def _upstream_ref_path(git_dir: str, upstream: str) -> str:
        """Map a ``# branch.upstream`` value to its loose remote-tracking ref.

        ``"origin/main"`` → ``<git_dir>/refs/remotes/origin/main``. Branch
        names with slashes (``origin/feature/x``) split into nested path
        components correctly. Returns ``""`` when there is no upstream. The
        returned path may not exist on disk (the ref could be packed) — the
        caller filters on existence and watches ``packed-refs`` as the
        companion trigger for that case.
        """
        if not upstream:
            return ""
        return os.path.join(git_dir, "refs", "remotes", *upstream.split("/"))

    @staticmethod
    def _current_branch_ref(git_dir: str) -> str:
        """Read ``.git/HEAD`` and return the path of the current branch's ref.

        On a detached HEAD, HEAD contains a commit hash instead of ``ref: …``
        — we return empty string and skip watching a ref file (the HEAD watch
        still catches movement).
        """
        head_path = os.path.join(git_dir, "HEAD")
        try:
            with open(head_path, encoding="utf-8") as f:
                content = f.read().strip()
        except OSError:
            return ""
        if not content.startswith("ref: "):
            return ""
        rel_ref = content[len("ref: ") :]
        return os.path.join(git_dir, rel_ref)

    def _clear_watcher(self) -> None:
        # Guard against calling removePaths([]). Qt's docs say it's a no-op,
        # but Qt 6 actually emits `QFileSystemWatcher::removePaths: list is
        # empty` warnings at runtime — visible in the IDE's startup log
        # every time this runs against an empty watcher (which is most
        # invocations, since most repo switches happen before the watcher
        # has armed any paths).
        # Regression note: a previous "simplification" removed these guards
        # trusting the docs; restore-with-comment after observing the log
        # noise so the next reviewer doesn't re-collapse them.
        files = self._watcher.files()
        if files:
            self._watcher.removePaths(files)
        dirs = self._watcher.directories()
        if dirs:
            self._watcher.removePaths(dirs)

    @Slot(str)
    def _on_watched_changed(self, path: str) -> None:
        """Handle a fileChanged event from the watcher.

        Re-arms the watch if the file was atomically replaced (the inode the
        watcher held is now unlinked; the new file at the same path is a
        different inode, so QFileSystemWatcher silently stops firing on it).
        Then starts the debounce timer — bursts of events collapse to one
        scan per burst.
        """
        if os.path.exists(path) and path not in self._watcher.files():
            self._watcher.addPath(path)
        self._debounce.start()

    @Slot(str)
    def _on_watched_dir_changed(self, path: str) -> None:
        """A watched directory changed — re-scan once a `.git` exists.

        Only the not-a-repo sentinel watches a directory, so this fires
        solely while we're waiting for a repo to appear. Gate on `.git`
        existing so unrelated top-level churn during scaffolding (pnpm
        install, file creation) doesn't fork `git rev-parse` on every write
        — `.git` may be a dir (ordinary repo) or a file (worktree/submodule),
        so test `exists`, not `isdir`. The partial-init race (`.git`
        mid-creation) is covered by the backstop, not this gate. Debounce
        coalesces; the woken scan re-resolves and arms the real watchers.
        """
        if os.path.exists(os.path.join(path, ".git")):
            self._debounce.start()

    @Slot()
    def _on_worktree_changed(self) -> None:
        """Working-tree change observed — debounce into one scan.

        Queued from the WorktreeWatcher's emitter thread; runs on the GUI
        thread where `_debounce` lives. Same coalescing path the `.git`
        trigger files use, so a burst of editor saves + the resulting
        index refresh still collapse to a single scan.
        """
        self._debounce.start()

    def poke(self) -> None:
        """External request for a debounced re-scan. GUI thread only.

        Wired to the nvim `gitpoke` capsule (BufWritePost) in
        AppController — an editor save is the highest-signal "status
        probably changed" event we have, and this path stays live even
        when the working-tree observer degraded (watchdog missing,
        inotify watch exhaustion).
        """
        self._debounce.start()

    def is_path_ignored(self, absolute_path: str) -> bool:
        """Thread-safe gitignore membership test for the watcher's filter.

        True when `absolute_path` or any of its ancestors (up to, not
        including, the resolved repo root) is in the ignored set. The
        ancestor walk is required because `_run_ignored_set` aggregates
        ignored DIRECTORIES into a single entry (`--directory`) — a file
        inside `node_modules` is not itself listed, only `node_modules`.

        Called from the watchdog emitter thread: snapshot-by-reference
        under the lock is safe because `_publish` replaces `_ignored_set`
        wholesale, never mutates it in place (same invariant
        `changedPathSet` relies on).

        Unknown-repo conservatism: with no resolved root (first scan not
        landed yet) nothing is treated as ignored — a spurious extra scan
        is cheaper than a missed real change.
        """
        with self._lock:
            ignored = self._ignored_set
            root = self._resolved_root
        if not root or not ignored:
            return False
        path = absolute_path.rstrip("/")
        root = root.rstrip("/")
        # Separator-bounded containment check — a bare startswith(root)
        # would also admit string-prefix siblings (`/repo-other` under
        # root `/repo`) into the ancestor walk.
        if not path.startswith(root + "/"):
            return False
        while len(path) > len(root):
            if path in ignored:
                return True
            path = os.path.dirname(path)
        return False

    def _wake_worker(self) -> None:
        """Set the wakeup event so the worker exits its wait and scans."""
        self._scan_wakeup.set()

    @Slot()
    def _on_sentinel_backstop(self) -> None:
        """Re-resolve while not-a-repo; back off and re-arm if still none.

        Wakes the worker for a full scan (which re-resolves the root). If the
        directory became a repo, `_refresh_watcher_for_root` cancels this
        timer and arms the real watchers; otherwise we re-arm at a doubled
        delay (capped at `_SENTINEL_BACKSTOP_MAX_MS`) so a permanent non-repo
        directory settles to a cheap once-a-minute poll. GUI-thread only
        (QTimer.timeout).
        """
        # The lock guards only `_resolved_root` (worker-written). `_repo_root`
        # is GUI-thread-owned — its sole writer `set_repo_root` runs here on
        # the GUI thread too — so it's read outside the lock (matching
        # `_arm_repo_sentinel`); reading it under the lock would imply a
        # cross-thread guarantee that doesn't exist.
        with self._lock:
            resolved = self._resolved_root
        has_root = bool(self._repo_root)
        if resolved or not has_root:
            # Sentinel/worktree path already won the race, or there's no root
            # left to watch — let the timer lapse.
            self._sentinel_backstop_delay_ms = 0
            return
        self._wake_worker()
        self._sentinel_backstop_delay_ms = min(
            self._sentinel_backstop_delay_ms * 2, _SENTINEL_BACKSTOP_MAX_MS
        )
        self._sentinel_backstop.start(self._sentinel_backstop_delay_ms)


# ---------------------------------------------------------------------------
# GitStatusListModel — flat projection of GitController for the panel.
# ---------------------------------------------------------------------------


class GitStatusListModel(QAbstractListModel):
    """Flat list of changed files, projected from ``GitController``.

    Filters directory aggregates (``char == '·'``) out — they live in the
    file tree's per-row badges, not in the changes panel which is a flat
    list of *files* the user has touched. Sorted alphabetically by
    repo-relative path for stable ordering across scans.

    Auto-refreshes on the controller's ``statusChanged`` signal. The panel
    binds ``visible: gitStatusList.count > 0`` so the section disappears
    when the tree is clean or we're not in a git repo.

    Roles (kebab-case bytes for QML):

      - ``path``         — absolute filesystem path (for click-to-jump)
      - ``displayName``  — repo-relative path (the row's label)
      - ``statusChar``   — single-character badge ("M", "?", "A", …)
      - ``statusState``  — semantic state name (for color lookup in QML)
      - ``tooltip``      — human-readable hover text
      - ``additions``    — int, lines added (0 if unknown / binary)
      - ``deletions``    — int, lines removed (0 if untracked / binary)
    """

    PathRole = Qt.ItemDataRole.UserRole + 1
    DisplayNameRole = Qt.ItemDataRole.UserRole + 2
    CharRole = Qt.ItemDataRole.UserRole + 3
    StateRole = Qt.ItemDataRole.UserRole + 4
    TooltipRole = Qt.ItemDataRole.UserRole + 5
    AdditionsRole = Qt.ItemDataRole.UserRole + 6
    DeletionsRole = Qt.ItemDataRole.UserRole + 7

    countChanged = Signal()

    def __init__(
        self,
        controller: GitController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._items: list[dict[str, object]] = []
        # The controller emits `statusChanged` on the worker thread when a
        # scan completes (the cross-thread emit is wrapped in gc.disable
        # per gotcha #10). Receivers MUST use QueuedConnection to hop onto
        # the GUI thread before mutating the model — direct connection
        # would call `beginResetModel` from the worker, which crashes Qt's
        # model/view invariants.
        # queued: GitController worker → GitStatusListModel GUI (§4 P2)
        self._controller.statusChanged.connect(
            self._refresh,
            Qt.ConnectionType.QueuedConnection,
        )

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.PathRole: b"path",
            self.DisplayNameRole: b"displayName",
            self.CharRole: b"statusChar",
            self.StateRole: b"statusState",
            self.TooltipRole: b"tooltip",
            self.AdditionsRole: b"additions",
            self.DeletionsRole: b"deletions",
        }

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008 — QModelIndex() default required by Qt; ARG002 — parent unused per QAbstractListModel contract
        return len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._items):
            return None
        item = self._items[index.row()]
        if role == self.PathRole:
            return item["path"]
        if role == self.DisplayNameRole:
            return item["displayName"]
        if role == self.CharRole:
            return item["char"]
        if role == self.StateRole:
            return item["state"]
        if role == self.TooltipRole:
            return item["tooltip"]
        if role == self.AdditionsRole:
            return item["additions"]
        if role == self.DeletionsRole:
            return item["deletions"]
        return None

    @Property(int, notify=countChanged)
    def count(self) -> int:
        """Row count exposed as a bindable property.

        QML's `ListView.count` exists but isn't directly bindable in older
        Qt versions; exposing our own `count` Property with `countChanged`
        notify makes `visible: model.count > 0` work universally.
        """
        return len(self._items)

    @Slot()
    def _refresh(self) -> None:
        """Rebuild items from the controller and emit modelReset.

        Runs on the GUI thread (the controller's signal is queued). A full
        reset is correct here because we don't know which entries moved;
        for typical change-set sizes (tens of files) a reset is cheaper
        than computing a diff, and the panel re-binds in one pass.
        """
        # Item dicts are heterogeneous now (str | int) — the older
        # `dict[str, str]` annotation no longer holds since additions /
        # deletions are ints. Using `dict[str, object]` so downstream
        # readers don't get a misleading type cue.
        new_items: list[dict[str, object]] = []
        for abs_path, status in self._controller._file_entries():
            new_items.append(
                {
                    "path": abs_path,
                    "displayName": status.path,
                    "char": status.char,
                    "state": status.state,
                    "tooltip": status.tooltip,
                    "additions": status.additions,
                    "deletions": status.deletions,
                }
            )
        if new_items == self._items:
            # Identical map — common when an unrelated `.git/` file changes
            # (e.g. fsmonitor cache touch). Skipping the emit avoids a
            # spurious modelReset that would invalidate every visible
            # delegate binding for no reason.
            return
        self.beginResetModel()
        old_count = len(self._items)
        self._items = new_items
        self.endResetModel()
        if old_count != len(self._items):
            self.countChanged.emit()
