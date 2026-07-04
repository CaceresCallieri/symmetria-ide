#!/usr/bin/env python3
"""Symmetria IDE agent reporter — forwards Claude-Code hook events to the IDE.

The IDE-owned counterpart to the shell's `symmetria-agent-hook.py`. It is
injected per-agent via `claude --settings '<json>'` (the IDE builds that JSON in
agent_harness/AppController) so every lifecycle event of an IDE-spawned Claude
agent reports DIRECTLY to the IDE that owns it — no shell-bridge round-trip. This
is the inbound edge of the agent-ownership inversion (docs/agent-ownership-inversion.md).

It is deliberately DUMB: it does no event→state mapping (that moved into the
IDE's `agent_activity.py`). It reads the raw hook JSON from stdin, picks out the
fields the state machine needs, and writes ONE JSON line to the IDE's agent
socket. All interpretation happens in the long-lived IDE process, which has the
per-agent memory (subagent depth, ordering) a one-shot hook cannot keep.

Discovery is purely environmental, so a single shared `--settings` string serves
every agent:
  - SYMMETRIA_AGENT_ID       — the agent's `<ide_pid>_<slot>` id (per-agent; set
                               by spawn_argv's env wrapper, inherited by claude).
  - SYMMETRIA_IDE_AGENT_SOCK — the owning IDE's agent-socket path (per-IDE).

Both come from the agent process's environment; the settings string carries only
this script's path (and, for the Notification hook, the `idle-notification` argv
marker). Missing either env var → silent no-op (the agent simply isn't IDE-owned).

Fire-and-forget and fast: a short socket timeout keeps a hook from ever stalling
a Claude turn, and the exit code is ALWAYS 0 — a reporter failure must never
break the agent.

Claude-Code 2.1.x daemon caveat: `SYMMETRIA_IDE_AGENT_SOCK` (and
`SYMMETRIA_AGENT_ID`) are FROZEN to the daemon spare-pool creator, so trusting
the env socket misroutes an agent's events to a different project's IDE. We
therefore RE-RESOLVE the target socket from the cross-IDE registry
(`symmetria_ide.agent_registry`) using the reliable signals — the stdin
`session_id` and this process's real `os.getcwd()` — and only fall back to the
frozen env socket when the registry has no answer. See agent_registry.py.
"""

import json
import os
import socket
import sys
import time

# Short enough that a hung/absent IDE socket never stalls a Claude turn (the hook
# is registered async, but a blocking connect would still hold the hook process).
_SOCKET_TIMEOUT_SECONDS = 1.0


def _resolve_target_socket(session_id: str, cwd: str, env_sock: str) -> str:
    """Owning IDE's socket via the registry; `env_sock` when it can't answer.

    Imports the package's pure `agent_registry` (Qt-free) by adding the repo's
    `src/` to sys.path — the hook lives at `<repo>/runtime/…`, so `../src` is the
    package root for whichever tree (dev or stable) injected this reporter. Any
    failure (import, bad registry) degrades to the frozen env socket — the
    pre-daemon behaviour — because a reporter must never break a turn.
    """
    try:
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        from symmetria_ide.agent_registry import resolve_socket

        routed = resolve_socket(session_id, cwd)
        return routed or env_sock
    except Exception:
        return env_sock


def main() -> None:
    agent_id = os.environ.get("SYMMETRIA_AGENT_ID", "")
    sock_path = os.environ.get("SYMMETRIA_IDE_AGENT_SOCK", "")
    if not agent_id or not sock_path:
        return  # not an IDE-owned agent, or no socket to report to

    raw = sys.stdin.read().strip()
    if not raw:
        return
    event = json.loads(raw)
    if not isinstance(event, dict):
        return  # a non-object payload has no hook fields — mirror the server-side guard

    session_id = event.get("session_id", "")
    # This hook process runs in the SESSION's real cwd, so os.getcwd() is a
    # reliable project signal even when SYMMETRIA_AGENT_ID is frozen by the
    # daemon. The IDE uses it to claim an agent's first event to the right slot.
    cwd = os.getcwd()

    # Curated passthrough — exactly the fields agent_activity.AgentActivityMachine
    # reads. We forward raw values (no mapping) so all interpretation stays in the
    # IDE. `event_ts_ns` uses CLOCK_REALTIME (time.time_ns) because monotonic
    # clocks are not comparable across the separate hook processes that race over
    # the socket; the IDE uses it only for out-of-order LOGGING.
    payload = {
        "type": "hook",
        "agent_id": agent_id,
        "hook_event_name": event.get("hook_event_name", ""),
        "session_id": session_id,
        # cwd lets the IDE derive the true slot when the frozen agent_id lies.
        "cwd": cwd,
        "tool_name": event.get("tool_name", ""),
        "permission_mode": event.get("permission_mode", ""),
        "source": event.get("source", ""),
        # is_interrupt is undocumented (a mid-tool Esc may surface here); forward
        # it raw so the IDE can decide. Absent → the IDE keeps normal behaviour.
        "is_interrupt": event.get("is_interrupt", False),
        # The Notification(idle_prompt) hook is registered with this argv marker
        # so the IDE can promote that specific Notification to "idle" without
        # guessing at undocumented payload fields.
        "idle_notification": "idle-notification" in sys.argv[1:],
        "event_ts_ns": time.time_ns(),
    }

    # Route to the OWNING IDE's socket (registry), not the frozen env socket.
    target = _resolve_target_socket(session_id, cwd, sock_path)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(_SOCKET_TIMEOUT_SECONDS)
        sock.connect(target)
        sock.sendall((json.dumps(payload) + "\n").encode())


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Hook failures must never block Claude Code — swallow everything and
        # exit 0. (No fallback log file: the IDE side logs what it receives, and
        # a broken reporter is visible there as missing events.)
        pass
