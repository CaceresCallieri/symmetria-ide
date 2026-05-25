"""Per-project expanded-state cache for the side-panel file tree.

Persists the set of directory paths the user has expanded in
`FileTreeView` to disk, keyed by a digest of the repo root's
absolute path. On project re-open, the IDE loads the saved set
and feeds it back to the FM via `restoreExpandedPaths`, which
restores the same tree shape the user left.

State file layout (`$XDG_STATE_HOME/symmetria-ide/projects/<hash>.json`):

    {
        "version": 1,
        "repo_root": "/home/jc/work/sales/bambin",
        "saved_at": "2026-05-25T14:32:10Z",
        "expanded_paths": ["/abs/path/1", "/abs/path/2", ...]
    }

- `<hash>` is a sha256 digest of the absolute `repo_root` truncated
  to 16 hex chars. Stable per-path, collision-resistant for a
  per-user state dir (NOT a security boundary — never depended on
  for trust decisions).
- Paths are absolute and stored as a sorted list so writes diff
  cleanly under `git status -s ~/.local/state/` if a user happens
  to track that dir.
- `repo_root` field is informational — used only for a sanity
  log warning if the saved value drifts from the requested one.
- Atomic write via `os.replace`: write a temp file in the same
  dir, then rename. A crash between the write and the rename
  leaves the original cache file untouched. The rename itself
  is atomic on POSIX (same filesystem).
- Load is fault-tolerant: missing file, unreadable file, malformed
  JSON, schema-version > known, and absent expected fields all
  resolve to an empty result (treated as "no cache, start fresh").
- Stale-path pruning on load: each saved path is stat'd; missing
  ones are dropped. The pruned set is what callers see; the
  pruned-out paths only disappear from disk on the next save.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Bump when the on-disk schema changes in a breaking way. Files with
# `version > KNOWN_VERSION` are dropped (treated as empty cache) so
# downgrades don't corrupt forward state.
KNOWN_VERSION = 1


def _state_dir() -> Path:
    """Resolve the directory holding per-project cache files.

    Honours `$XDG_STATE_HOME`; falls back to `~/.local/state` per
    the XDG base directory spec. The `projects/` subfolder is
    created on demand — callers don't need to mkdir.
    """
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    out = Path(base) / "symmetria-ide" / "projects"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _repo_hash(repo_root: str) -> str:
    """Stable filename digest for a repo root path.

    NOT a security primitive — collision-resistant enough that the
    per-user state dir won't see filename clashes for any realistic
    number of projects.
    """
    return hashlib.sha256(repo_root.encode("utf-8")).hexdigest()[:16]


def _cache_path(repo_root: str) -> Path:
    return _state_dir() / f"{_repo_hash(repo_root)}.json"


def load_expanded(repo_root: str) -> list[str]:
    """Return the saved expanded-paths list for `repo_root`, pruned.

    Missing file / unreadable file / malformed JSON / unknown
    schema version all resolve to `[]`. Paths that no longer exist
    on disk are dropped before return — callers see only live paths.
    """
    if not repo_root:
        return []
    path = _cache_path(repo_root)
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as e:
        log.warning("tree_state_cache: load failed for %s: %s", repo_root, e)
        return []

    if not isinstance(data, dict):
        return []
    version = data.get("version")
    if not isinstance(version, int) or version > KNOWN_VERSION or version < 1:
        return []
    raw = data.get("expanded_paths")
    if not isinstance(raw, list):
        return []

    # Stat-prune: drop paths that have been deleted/renamed since save.
    pruned: list[str] = []
    for p in raw:
        if isinstance(p, str) and os.path.isdir(p):
            pruned.append(p)
    return pruned


def save_expanded(repo_root: str, paths: list[str]) -> None:
    """Atomically persist `paths` for `repo_root`.

    Empty `paths` is a valid input — it writes a cache file with an
    empty list (rather than deleting the file). That keeps the
    `repo_root` + `saved_at` provenance for future debugging, and
    lets callers distinguish "user collapsed everything" from
    "user has never opened this project".
    """
    if not repo_root:
        return
    target = _cache_path(repo_root)
    payload = {
        "version": KNOWN_VERSION,
        "repo_root": repo_root,
        "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # Sorted for a stable on-disk representation across saves with
        # the same logical set — keeps diffs small if the file is ever
        # version-controlled and avoids spurious change detection by
        # external watchers.
        "expanded_paths": sorted(set(p for p in paths if isinstance(p, str))),
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    # `dir=` is load-bearing: `os.replace` is atomic only across paths
    # on the same filesystem, and `tempfile`'s default tmp dir might
    # live on a different FS than the state dir.
    tmp_fd = None
    tmp_path = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".tree-state-",
            suffix=".json.tmp",
            dir=str(target.parent),
        )
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            tmp_fd = None  # ownership passed to the file object
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
        tmp_path = None
    except OSError as e:
        log.warning("tree_state_cache: save failed for %s: %s", repo_root, e)
    finally:
        # Defensive cleanup if either the open or the replace raised
        # before we relinquished ownership. Errors here are silent —
        # a leaked tmp file in the state dir is harmless.
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
