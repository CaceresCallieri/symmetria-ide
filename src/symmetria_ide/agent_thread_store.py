"""Per-project durable metadata for agent threads — the IDE's own half.

The harnesses already record everything about a conversation that they own:
claude keeps a transcript per session, opencode keeps a session store. This
file holds the ONE fact neither of them records and only the IDE can observe —
``work_root``, the repo root of the last file a WRITE tool touched, which is
what makes "resume this thread where it was working" possible.

Deliberately nothing else about the CONVERSATION. Titles, message history and
the harness's own session timestamps stay with the harness; duplicating them
here would create a second truth that drifts the moment a session continues
outside the IDE. The one extra field, ``updated_at``, is provenance for OUR
write — when this file last learned the root — not a harness-owned value, and
it is the same informational stamp ``tree_state_cache`` keeps as ``saved_at``.

Why the session cwd cannot serve instead (measured, 2026-08-15): a session
whose ``cwd`` is the main checkout made 158 of its 230 writes inside a linked
worktree. A Bash-tool ``cd`` moves the tool's persistent shell, never the
session's ``os.getcwd()`` — so only the write-tool path tracks the real work
location, and only the IDE sees it, live, through the reporter hooks.

File layout (``$XDG_STATE_HOME/symmetria-ide/agent-threads/<hash>.json``)::

    {
      "version": 1,
      "root": "/home/jc/projects/symmetria-ide",
      "threads": {
        "claude:<session_id>": {"work_root": "/…/wt-1", "updated_at": 1755…}
      }
    }

- ``<hash>`` digests the CANONICAL project root, so a linked worktree and its
  main checkout share one file — the user's "project", not a directory (the
  same folding ``canonical_project_root`` does for the registry).
- The key is ``"<harness>:<session_id>"``, the durable thread identity: a
  session id is only unique WITHIN a harness, so the harness prefix is what
  keeps a claude and an opencode session of the same name apart.
- Every save is READ-MERGE-WRITE, never a bare overwrite. The file holds one
  entry per thread of the project and each write knows about exactly one of
  them — the same discipline ``project_browser_marker.set_browser_agents``
  needed once its marker grew a second field. That sequence is held under
  ``fs_atomic.write_lock``: the canonical key means a worktree and its main
  checkout share this file, so two IDE windows are the NORMAL case here, and
  an unlocked merge would lose one entry silently — a thread whose root went
  missing resumes in the main checkout down the "never recorded" branch, which
  says nothing to the user.
- Load is fault-tolerant in the shape the other state files use: missing,
  unreadable, malformed, non-dict, or a forward version all mean "no state".
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .fs_atomic import atomic_write_json, write_lock
from .project_browser_marker import resolve_project_root
from .state_paths import repo_hash, state_dir
from .worktree import canonical_project_root

log = logging.getLogger(__name__)

# Bump on a breaking on-disk change. A file claiming a HIGHER version is
# dropped rather than parsed, so a downgrade cannot corrupt forward state.
KNOWN_VERSION = 1


def thread_key(harness: str, session_id: str) -> str:
    """The durable identity `"<harness>:<session_id>"` used everywhere.

    Written once here so the store, the rail's `ThreadRow.thread_id` and
    `resume_thread`'s lookup cannot disagree about the separator.
    """
    return f"{harness}:{session_id}"


def project_key(project_root: str) -> str:
    """The canonical root a project's threads are filed under.

    `resolve_project_root` first (a caller may hand us a subdirectory), then
    `canonical_project_root` so a linked worktree files onto its main
    checkout. Both are pure path math — neither touches git nor the network.
    """
    return canonical_project_root(resolve_project_root(project_root))


def _store_path(project_root: str) -> Path:
    return state_dir("agent-threads") / f"{repo_hash(project_root)}.json"


def _load_document(project_root: str) -> dict:
    """The whole on-disk document for a canonical root; `{}` on any failure."""
    if not project_root:
        return {}
    path = _store_path(project_root)
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        log.warning("agent_thread_store: load failed for %s: %s", path, e)
        return {}
    if not isinstance(data, dict):
        return {}
    version = data.get("version")
    if not isinstance(version, int) or version < 1 or version > KNOWN_VERSION:
        return {}
    return data


def load_threads(project_root: str) -> dict[str, dict]:
    """Every stored thread entry for `project_root`, keyed by `thread_key`."""
    threads = _load_document(project_key(project_root)).get("threads")
    if not isinstance(threads, dict):
        return {}
    return {
        key: entry
        for key, entry in threads.items()
        if isinstance(key, str) and isinstance(entry, dict)
    }


def load_work_root(project_root: str, harness: str, session_id: str) -> str | None:
    """The stored work root for one thread, or `None` when we never saw it.

    `None` rather than `""` is the contract: it separates "this store has no
    entry for the thread" from "an entry exists and records an empty root",
    which is what the read-merge-write in `save_work_root` needs in order to
    leave an existing entry alone rather than overwrite it with nothing.

    (It used to be justified by the resume path treating the two differently —
    silent fallback vs. a warning. It no longer does: `_resume_start_root`
    alerts on every falsy value, because OpenCode records no work location at
    all and silence there would hide the ordinary case.)
    """
    if not harness or not session_id:
        return None
    entry = load_threads(project_root).get(thread_key(harness, session_id))
    if not isinstance(entry, dict):
        return None
    value = entry.get("work_root")
    return value if isinstance(value, str) and value else None


def save_work_root(
    project_root: str, harness: str, session_id: str, work_root: str
) -> bool:
    """Persist one thread's work root; `True` when the file was written.

    A missing project root, harness or session id is a NO-OP returning
    `False` — an agent that has not revealed its session id yet has no
    durable identity to file anything under, and this is called from the
    live hook path where that is the ordinary early state, not an error.
    """
    if not project_root or not harness or not session_id or not work_root:
        return False
    root = project_key(project_root)
    # Read-merge-write under the cross-process mutex: every other thread of
    # this project lives in the same file, this call knows about exactly one
    # of them, and a sibling IDE window on the same canonical root writes to
    # the same path (see the module docstring).
    with write_lock(_store_path(root)):
        document = _load_document(root)
        threads = document.get("threads")
        if not isinstance(threads, dict):
            threads = {}
        merged = {
            key: entry
            for key, entry in threads.items()
            if isinstance(key, str) and isinstance(entry, dict)
        }
        merged[thread_key(harness, session_id)] = {
            "work_root": work_root,
            # Provenance for this write, not a harness value — see the
            # module docstring's note on what is deliberately absent.
            "updated_at": int(time.time()),
        }
        return atomic_write_json(
            _store_path(root),
            {"version": KNOWN_VERSION, "root": root, "threads": merged},
        )
