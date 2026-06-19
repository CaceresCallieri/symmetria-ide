"""Per-project opt-in for the agent browser capability.

A project either does web work (its agents should get the browser MCP
tools — `browser_open` + the off-the-shelf `chrome-devtools-mcp`) or it
doesn't (e.g. this IDE itself: native Qt/QML, no web surface). The
distinction is a PROJECT property, so it lives in a **committable marker**
at the repo root:

    <repo>/.symmetria/ide.json
    { "version": 1, "browser_agents": true }

Committing it means "this is a web project" travels with the repo — clone
it elsewhere and its agents get browser tools automatically, no per-machine
setup. Absent file / `browser_agents` not `true` ⇒ disabled (the default),
so a non-web project's agents spawn with NO browser MCP and thus NO
per-agent `npx chrome-devtools-mcp` Node process (the RAM the gate saves).

Pure, synchronous, Qt-free (unit-testable like `agent_harness`):
`AppController` reads `browser_agents_enabled()` to gate
`browser_mcp.agent_config_path`, and flips it via `set_browser_agents()`
behind the MCP-toggles popup (`Ctrl+Shift+M` → `w`).

Root resolution mirrors how git itself finds a repo: walk up from the
launch dir to the first ancestor holding a `.git` (dir OR file — worktrees
and submodules use a `.git` file) or an existing `.symmetria/`. No
subprocess, and it sidesteps GitController's async `_resolved_root` timing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .fs_atomic import atomic_write_json

log = logging.getLogger(__name__)

# Bumped only if the marker schema changes in a breaking way. WRITTEN on
# every save for future migrations, but deliberately NOT enforced on READ:
# the marker is committed and shared across machines that may run different
# IDE versions, so the contract is the `browser_agents` field, not the
# version. A newer IDE only adds fields; an older one must still honour a
# `browser_agents: true` it finds. (Contrast tree_state_cache, which is a
# machine-local cache and rejects forward versions to protect downgrades.)
MARKER_VERSION = 1

# `.symmetria/ide.json` — a conventional dotfolder at the repo root, like
# `.vscode/` or `.idea/`. A folder (not a bare `.symmetria-ide` file) so
# future per-project IDE settings have a home beside this one.
MARKER_DIR = ".symmetria"
MARKER_FILE = "ide.json"


def resolve_project_root(start: str) -> str:
    """Return the repo root for `start`, or `start` itself if none is found.

    Walks up from `start` to the first ancestor directory containing a
    `.git` entry (dir or file) or an existing `.symmetria/`. Returns "" for
    an empty/blank input. Symlinks are NOT resolved — the user-facing path
    is preserved so a committed marker sits where the user expects.
    """
    if not start:
        return ""
    current = Path(start).expanduser()
    # `current` first, then each ancestor up to the filesystem root.
    for d in (current, *current.parents):
        if (d / ".git").exists() or (d / MARKER_DIR).is_dir():
            return str(d)
    return str(current)


def marker_path(project_root: str) -> Path:
    """Path to the marker file for a resolved `project_root`."""
    return Path(project_root) / MARKER_DIR / MARKER_FILE


def browser_agents_enabled(start: str) -> bool:
    """True iff the project owning `start` has opted into agent browser tools.

    Fault-tolerant by design — a missing file, unreadable file, malformed
    JSON, or wrong shape all resolve to `False` (the safe default: agents
    get no browser MCP). Only an explicit `browser_agents: true` enables it.
    """
    root = resolve_project_root(start)
    if not root:
        return False
    path = marker_path(root)
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return False
    except (OSError, json.JSONDecodeError) as e:
        log.warning("project_browser_marker: read failed for %s: %s", path, e)
        return False
    if not isinstance(data, dict):
        return False
    return data.get("browser_agents") is True


def set_browser_agents(start: str, enabled: bool) -> str:
    """Write the marker for the project owning `start`; return its root.

    Resolves the repo root, creates `.symmetria/` if needed, and atomically
    writes `{"version": MARKER_VERSION, "browser_agents": <enabled>}`.
    Returns the resolved root on success (for logging / the toggle's
    feedback), or "" when the input is blank or the write fails.
    """
    root = resolve_project_root(start)
    if not root:
        return ""
    payload = {"version": MARKER_VERSION, "browser_agents": bool(enabled)}
    if atomic_write_json(marker_path(root), payload):
        return root
    return ""
