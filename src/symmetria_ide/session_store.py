"""Per-project saved-session manifest — the IDE's sessionizer storage layer.

Persists a snapshot of one project's workspace (the running agents + their
resumable harness session ids, the open browser windows, the visible central
surface, anchor state, and the open editor files) so the IDE can rebuild it
later. Two consumers, both in `AppController`:

  - **Reload-in-place**: save on quit, ``os.execvpe`` the (possibly updated)
    binary, and auto-restore — picking up a new build without losing the
    workspace.
  - **Cold relaunch**: the saved session is *offered* (a small indicator +
    ``Ctrl+Shift+S``), never forced.

Storage mirrors `tree_state_cache`: one JSON file per project under
``$XDG_STATE_HOME/symmetria-ide/sessions/<hash>.json`` (path math shared via
`state_paths`), an atomic write via `fs_atomic.atomic_write_json`, and a
fault-tolerant load that treats any missing / unreadable / malformed /
forward-version file as "no session" (returns ``None``).

The manifest is keyed by the project root the caller passes in — which MUST be
``AppController.displayedRoot`` (the anchored/displayed root), NOT
``os.getcwd()``: the two diverge once a project is anchored, and saving under
one key while loading under the other would silently lose the session.

Schema (version 1) — the caller (`AppController`) owns the field semantics;
this module only stamps ``version`` / ``root`` / ``saved_at`` and round-trips
the rest verbatim::

    {
      "version": 1,
      "root": "/abs/project",
      "saved_at": "2026-06-25T14:32:10Z",
      "anchored": false,
      "central_surface": "agent",
      "focused_agent": 1,
      "focused_browser": 0,
      "agents": [
        {"slot": 1, "display_order": 1, "harness": "claude",
         "spawn_type": "resume", "session_id": "<uuid>",
         "dangerous": true, "title": "..."}
      ],
      "browsers": [{"order": 1, "url": "https://..."}],
      "editor": {"active": "/abs/file.py", "line": 42, "col": 3,
                 "files": ["/abs/a.py", "/abs/b.py"]}
    }

This module is a dumb storage layer: it does NOT decide when a session is
"empty enough" to skip — that policy (so the cold-launch indicator stays
honest) lives in `AppController.save_session`, which calls `save` or `delete`
accordingly.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .fs_atomic import atomic_write_json
from .state_paths import repo_hash, state_dir

log = logging.getLogger(__name__)

# Bump when the on-disk schema changes in a breaking way. Files with
# `version > KNOWN_VERSION` (or `< 1`) are treated as "no session" so a
# downgrade never tries to restore a manifest it can't understand.
KNOWN_VERSION = 1


def _session_path(root: str) -> Path:
    return state_dir("sessions") / f"{repo_hash(root)}.json"


def load(root: str) -> dict | None:
    """Return the saved manifest for `root`, or ``None`` if there is none.

    Missing file / unreadable file / malformed JSON / non-dict top level /
    out-of-range schema version all resolve to ``None`` ("no session"). The
    returned dict is otherwise verbatim — the caller validates inner fields
    defensively at restore time (a partially-written session should degrade,
    not crash).
    """
    if not root:
        return None
    path = _session_path(root)
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        log.warning("session_store: load failed for %s: %s", root, e)
        return None

    if not isinstance(data, dict):
        return None
    version = data.get("version")
    if not isinstance(version, int) or version > KNOWN_VERSION or version < 1:
        return None
    return data


def save(root: str, manifest: dict) -> bool:
    """Atomically persist `manifest` for `root`.

    Stamps ``version`` / ``root`` / ``saved_at`` (overwriting any caller-set
    values so they can't drift), then round-trips the rest. Returns ``True``
    on success, ``False`` on an empty root or a filesystem failure (the
    latter logged by `atomic_write_json`). The caller decides whether to call
    this or `delete` based on whether the manifest is worth restoring.
    """
    if not root:
        return False
    payload = dict(manifest)
    payload["version"] = KNOWN_VERSION
    payload["root"] = root
    payload["saved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return atomic_write_json(_session_path(root), payload)


def exists(root: str) -> bool:
    """True iff a *restorable* session is on disk for `root`.

    Defined as ``load(root) is not None`` rather than a bare file check so a
    corrupt or forward-version file (which `load` rejects) does not light the
    cold-launch indicator — the indicator must promise only what restore can
    actually deliver.
    """
    return load(root) is not None


def delete(root: str) -> None:
    """Remove the saved session for `root` if present (idempotent).

    Used when the workspace is empty at save time, so a stale manifest from a
    previous session doesn't keep the indicator lit / offer a restore that no
    longer reflects reality.
    """
    if not root:
        return
    try:
        _session_path(root).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("session_store: delete failed for %s: %s", root, e)
