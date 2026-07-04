"""Cross-IDE agent-attribution registry — the fix for Claude Code's shared daemon.

Claude Code 2.1.x runs a per-user **daemon + pre-warmed spare pool** (one pool
dir under `/tmp/cc-daemon-1000/<id>/`). The real interactive session is a
grandchild the daemon hosts in a background PTY, NOT the `claude` process the IDE
spawned. Pre-warmed spares inherit the environment of whoever created the pool
FIRST, so `SYMMETRIA_AGENT_ID` and `SYMMETRIA_IDE_AGENT_SOCK` — the two env vars
the whole agent-ownership inversion keyed identity on — get FROZEN to the
pool-creator and then reused across unrelated projects/IDEs.

Symptom: agent B's activity reports to agent A's IDE under agent A's id. A's
sparkle lights for B's work; B's own IDE never hears about B. See CLAUDE.md
"The terminal-agent runtime" and the reference memory on the daemon break.

The reliable signals survive the daemon: the hook's stdin `session_id` is the
real per-session id, and the hook process runs in the session's real cwd
(`os.getcwd()`). Only the frozen env is poisoned. This module lets attribution
ride those reliable signals instead:

  - **Registry (on-disk, one file per IDE):** each IDE publishes
    `{pid, socket, project_root, sessions{session_id: slot}}` to
    `$XDG_RUNTIME_DIR/symmetria-ide/registry/<pid>.json`. The reporter hook
    (`runtime/symmetria-ide-agent-hook.py`) reads it to route each event to the
    OWNING IDE's socket — by session id when known, else by project root
    (one-IDE-per-project topology makes root→IDE effectively 1:1). This replaces
    trusting the frozen `SYMMETRIA_IDE_AGENT_SOCK`.

  - **`resolve_slot_for_event` (pure):** the receiving IDE derives the true slot
    from `session_id` + `cwd` instead of the poisoned `agent_id` prefix. First
    event for an unknown session claims the newest unbound slot in that project
    (the bootstrap; exact from then on).

Pure + filesystem-only — NO Qt import — so the reporter (a standalone script)
can import it, and so `resolve_slot_for_event` is unit-testable exactly like
`agent_activity.py` / `agent_harness.py`.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .fs_atomic import atomic_write_json
from .project_browser_marker import resolve_project_root

log = logging.getLogger(__name__)


def _runtime_dir() -> str:
    # Mirrors agent_events._runtime_dir — same base the agent sockets live under.
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"


def registry_dir() -> Path:
    """Directory holding one `<pid>.json` per live IDE instance."""
    return Path(_runtime_dir()) / "symmetria-ide" / "registry"


def _entry_path(pid: int) -> Path:
    return registry_dir() / f"{pid}.json"


def _pid_alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{int(pid)}").exists()
    except (TypeError, ValueError):
        return False


def write_entry(
    pid: int, socket_path: str, project_root: str, sessions: dict[str, int]
) -> None:
    """Publish (atomically) this IDE's routing entry.

    `sessions` maps each bound `session_id` → its pool slot. `project_root` is
    the resolved repo root the IDE currently displays — the bootstrap key for
    an agent's FIRST event (before its session id is known to anyone). Failure
    is swallowed by `atomic_write_json` (returns False, logged) — a missing
    entry just falls the reporter back to the frozen env socket (old behaviour).
    """
    atomic_write_json(
        _entry_path(pid),
        {
            "pid": int(pid),
            "socket": socket_path,
            "project_root": project_root,
            "sessions": sessions,
        },
    )


def remove_entry(pid: int) -> None:
    """Drop this IDE's entry on clean shutdown (best-effort)."""
    try:
        _entry_path(pid).unlink()
    except OSError:
        pass


def reap_dead() -> None:
    """Remove entries left by dead IDE instances (crash/SIGKILL cleanup).

    The filename encodes the writing IDE's pid, so an entry whose pid is no
    longer alive is safe to delete. Mirrors `agent_events.reap_orphan_sockets`.
    """
    d = registry_dir()
    if not d.is_dir():
        return
    for f in d.glob("*.json"):
        try:
            pid = int(f.stem)
        except ValueError:
            continue  # not a pid-named entry — leave it alone
        if pid == os.getpid() or _pid_alive(pid):
            continue
        try:
            f.unlink()
        except OSError:
            pass


def _read_entries() -> list[dict]:
    """All well-formed live-or-not registry entries (dead pids filtered later)."""
    d = registry_dir()
    if not d.is_dir():
        return []
    out: list[dict] = []
    for f in d.glob("*.json"):
        try:
            with f.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue  # unreadable / half-written — skip, don't crash the hook
        if isinstance(data, dict) and data.get("socket"):
            out.append(data)
    return out


def resolve_socket(session_id: str, cwd: str) -> str:
    """The owning IDE's agent socket for an event, or "" if none is found.

    Resolution order (most precise first):
      1. an entry whose `sessions` already knows `session_id` — exact, and
         robust even if the agent `cd`'d out of its launch project;
      2. an entry whose `project_root` equals `resolve_project_root(cwd)` — the
         bootstrap for an agent's first event, before its session is bound.

    Dead-pid entries are skipped (a stale file must not capture live events).
    "" lets the reporter fall back to the frozen `SYMMETRIA_IDE_AGENT_SOCK`.
    """
    entries = _read_entries()
    if session_id:
        for e in entries:
            sessions = e.get("sessions") or {}
            if session_id in sessions and _pid_alive(e.get("pid", -1)):
                return str(e["socket"])
    if cwd:
        root = resolve_project_root(cwd)
        for e in entries:
            if e.get("project_root") == root and _pid_alive(e.get("pid", -1)):
                return str(e["socket"])
    return ""


def resolve_slot_for_event(
    term_agents: dict[int, dict],
    my_pid: int,
    session_id: str,
    cwd: str,
    agent_id_env: str,
) -> tuple[int | None, str]:
    """Derive the true pool slot for a forwarded hook event (pure).

    `term_agents` is the controller's `{slot: {"session_id", "cwd",
    "spawn_mono", ...}}`. Returns ``(slot, session_id_to_bind)``:

      - ``slot`` is the resolved pool slot, or ``None`` if the event belongs to
        no local agent (drop it);
      - ``session_id_to_bind`` is the session id the caller should STORE on that
        slot (``""`` when nothing new needs binding) — mutation stays in the Qt
        caller so this function is pure.

    Order:
      1. **exact session match** — a slot already bound to `session_id`;
      2. **legacy env id** — `agent_id_env == "<my_pid>_<slot>"` (the un-poisoned
         path: an in-process agent whose env was never frozen). Binds the
         session id if the slot lacks one;
      3. **project-root claim** — an unknown session that the reporter routed to
         us by project root. Bind it to the newest unbound slot in the same
         project (the bootstrap; exact for every later event).
    """
    if session_id:
        for slot, inst in term_agents.items():
            if inst.get("session_id") == session_id:
                return slot, ""

    prefix = f"{my_pid}_"
    if agent_id_env.startswith(prefix):
        try:
            slot = int(agent_id_env[len(prefix) :])
        except ValueError:
            slot = None
        if slot is not None and slot in term_agents:
            needs_bind = bool(session_id and not term_agents[slot].get("session_id"))
            return slot, (session_id if needs_bind else "")

    if session_id and cwd:
        root = resolve_project_root(cwd)
        candidates = [
            slot
            for slot, inst in term_agents.items()
            if not inst.get("session_id")
            and resolve_project_root(inst.get("cwd", "")) == root
        ]
        if candidates:
            # Newest spawn wins — the slot the user most recently opened in this
            # project is the one whose first session is arriving now.
            slot = max(candidates, key=lambda s: term_agents[s].get("spawn_mono", 0))
            return slot, session_id

    return None, ""
