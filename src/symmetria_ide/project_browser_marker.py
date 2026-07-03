"""Per-project IDE marker — `.symmetria/ide.json`.

Owns the committable per-project config file the IDE reads at agent spawn.
Two concerns live here today (both PROJECT properties that should travel
with the repo, hence committed):

    <repo>/.symmetria/ide.json
    {
      "version": 1,
      "browser_agents": true,
      "harness_defaults": {
        "claude":   { "model": "opus", "effort": "high" },
        "opencode": { "model": "anthropic/claude-opus-4-8", "effort": "high" }
      }
    }

1. `browser_agents` — does this project do web work? Its agents get the
   browser MCP tools (`browser_open` + off-the-shelf `chrome-devtools-mcp`).
   Absent / not `true` ⇒ disabled (the default), so a non-web project's
   agents spawn with NO per-agent `npx chrome-devtools-mcp` Node process
   (the RAM the gate saves). Read via `browser_agents_enabled()`, flipped by
   `set_browser_agents()` behind the MCP-toggles popup (`Ctrl+Shift+M` → `w`).

2. `harness_defaults` — the model + reasoning-effort a NEW agent launches
   with, per harness (claude/opencode). Read via `harness_model_effort()`,
   threaded into `agent_harness.spawn_argv` as `--model`/`--effort` (claude)
   or `--model`/`--variant` (opencode). Absent ⇒ empty strings ⇒ the harness
   default (no flags appended). This solves the two-command `/model` +
   `/effort` friction: commit it once and every agent in the project starts
   correct.

Committing the file means these settings travel with the repo — clone it
elsewhere and its agents inherit them, no per-machine setup.

Pure, synchronous, Qt-free (unit-testable like `agent_harness`).

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


def read_marker(start: str) -> dict:
    """Return the parsed marker dict for the project owning `start`, or `{}`.

    The single fault-tolerant read every reader shares — a missing file,
    unreadable file, malformed JSON, or non-dict top level all resolve to an
    empty dict (each field reader then applies its own default). Keeping the
    read in one place is what guarantees `browser_agents_enabled` and
    `harness_model_effort` agree on root resolution and error handling.
    """
    root = resolve_project_root(start)
    if not root:
        return {}
    path = marker_path(root)
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        log.warning("project_browser_marker: read failed for %s: %s", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def browser_agents_enabled(start: str) -> bool:
    """True iff the project owning `start` has opted into agent browser tools.

    Fault-tolerant by design (via `read_marker`): missing / unreadable /
    malformed / wrong-shape all resolve to `False` (the safe default: agents
    get no browser MCP). Only an explicit `browser_agents: true` enables it.
    """
    return read_marker(start).get("browser_agents") is True


def harness_model_effort(start: str, harness_name: str) -> tuple[str, str]:
    """Return `(model, effort)` launch defaults for `harness_name`, or `("", "")`.

    Reads `harness_defaults[<harness_name>]` from the marker. Each value is
    returned ONLY when it is a non-empty string — any other type (or a missing
    key / missing block / wrong shape) yields "" for that axis, which
    `spawn_argv` treats as "append no flag" (harness default). Effort-VALUE
    validity (is "high" a real level for this harness?) is deliberately NOT
    checked here — that is harness knowledge, enforced in `spawn_argv` against
    the harness's `valid_efforts`. This reader only guarantees the types.
    """
    block = read_marker(start).get("harness_defaults")
    if not isinstance(block, dict):
        return "", ""
    entry = block.get(harness_name)
    if not isinstance(entry, dict):
        return "", ""

    def _str_field(key: str) -> str:
        value = entry.get(key)
        return value if isinstance(value, str) and value else ""

    return _str_field("model"), _str_field("effort")


def set_browser_agents(start: str, enabled: bool) -> str:
    """Flip `browser_agents` in the marker for `start`'s project; return root.

    Resolves the repo root, creates `.symmetria/` if needed, and atomically
    writes the marker. **Read-merge-write, NOT overwrite** — the existing
    marker is loaded and only `browser_agents` (plus `version`) is set, so any
    OTHER fields the user committed (notably `harness_defaults`) survive a
    browser toggle from the `Ctrl+Shift+M` popup. A bare overwrite here would
    silently wipe the model/effort config the moment both features share the
    file. Returns the resolved root on success (for logging / the toggle's
    feedback), or "" when the input is blank or the write fails.
    """
    root = resolve_project_root(start)
    if not root:
        return ""
    payload = read_marker(root)  # preserve harness_defaults + any unknown fields
    payload["version"] = MARKER_VERSION
    payload["browser_agents"] = bool(enabled)
    if atomic_write_json(marker_path(root), payload):
        return root
    return ""
