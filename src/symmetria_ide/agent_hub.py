"""Headless agent hub — consolidated agent state over a Unix socket, no desktop.

This is the PORTABLE half of the Symmetria agent-state system: the same
event→state brain (`agent_activity.AgentActivityMachine`) behind the same
subscriber wire protocol the Symmetria Shell's `agent-bridge.py` speaks, with
none of the desktop coupling (no QML stdout consumer, no nvim orchestrator
solicitation, no terminal-PID resolution). It exists so a headless machine — a
Vigilia server, a home box — can expose live agent activity to remote
frontends (vigiliad → the mobile app; later, an SSH-remote Symmetria IDE)
using ONE shared implementation instead of a per-host port of the state
machine. See the Vigilia repo's `docs/phase2-headless-hub-plan.md`.

Ingest side — dumb hook reporters connect, write one JSON line, disconnect:

    {"type": "hook", "agent_id": "<tmux session>", "hook_event_name": "...",
     "session_id": "...", "cwd": "...", "tool_name": "...",
     "permission_mode": "...", "source": "...", "is_interrupt": false,
     "idle_notification": false, "event_ts_ns": 123}

This is the exact payload `runtime/symmetria-ide-agent-hook.py` forwards; the
server-side reporter (Vigilia's `vigilia-agent-hook.py`) mirrors it with
`agent_id` = the tmux session name. All interpretation happens here, through
`AgentActivityMachine` — the hub never invents its own event semantics.

Subscriber side — long-lived consumers send `{"type": "subscribe"}` and then
receive one JSON snapshot line on every state change (plus one immediately on
subscribe). The snapshot schema is the compatible subset of the shell bridge's
`_snapshot_line`, so an existing bridge subscriber (e.g. vigiliad's
`agentHub`) works against either hub unchanged:

    {"agents": [{"id", "project", "title", "active", "remote", "spawned_at",
                 "agent_type", "activity_state", "activity_tool",
                 "in_plan_mode", "session_id", "tmux_session",
                 "last_prompt", "last_messages"}, ...],
     "projects": ["...", ...]}

Liveness. Hooks alone cannot observe two things, so the hub runs a reaper:

  - **Esc-cancel reconciliation**: Claude fires NO hook on a user interrupt,
    so a canceled agent looks busy forever. `claude agents --json` is the
    ground truth; a busy agent with no hook traffic for
    ``CLAUDE_RECONCILE_AFTER_SECONDS`` whose session the CLI explicitly
    reports idle gets its activity force-cleared. Ported verbatim (same tuned
    constants) from the shell bridge's `reconcile_claude_sessions`.
  - **Dead-session pruning**: a killed tmux session fires no SessionEnd. When
    ``--tmux-socket`` is given, agents whose tmux session (their id) no
    longer exists are dropped from the table.

Known limits (deliberate v1): one agent per tmux session (a second Claude in
the same session shares the id — last writer wins), and a Claude that exits
while its session's shell survives leaves an idle row until the session dies.

Runs as a package module (`python -m symmetria_ide.agent_hub`) or as a loose
script deployed side-by-side with `agent_activity.py` (no package install on
the server — Vigilia's deploy just copies both files).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from pathlib import Path

try:  # package import (python -m symmetria_ide.agent_hub / IDE-internal use)
    from .agent_activity import AgentActivityMachine
except ImportError:  # loose-script deployment: agent_activity.py sits alongside
    from agent_activity import AgentActivityMachine  # type: ignore[no-redef]

log = logging.getLogger("agent_hub")

# Esc-cancel reconciliation constants — same values as the shell bridge (they
# were tuned there against live Claude Code; see its CLAUDE_RECONCILE_* block).
CLAUDE_RECONCILE_AFTER_SECONDS = 15
CLAUDE_RECONCILE_MIN_INTERVAL = 10
CLAUDE_CLI_TIMEOUT_SECONDS = 10

# Reaper cadence (reconcile + tmux prune checks run on this tick).
REAPER_INTERVAL_SECONDS = 5

# Snapshot emit coalescing window — same leading+trailing edge scheme as the
# shell bridge (N events in a burst → at most 2 emissions).
EMIT_COALESCE_SECONDS = 0.05


class HeadlessAgentHub:
    """Agent table + state machine + subscriber fan-out, hook-driven only.

    Unlike the shell bridge there are no registered orchestrator clients:
    an agent EXISTS because its hook events say so (first event creates the
    row, SessionEnd or a tmux prune removes it).
    """

    def __init__(self, tmux_socket: str = "") -> None:
        self._machine = AgentActivityMachine()
        self._tmux_socket = tmux_socket
        # {agent_id: {"project": str, "spawned_at": int, "session_id": str,
        #             "last_event_mono": float}} — identity/sticky facts that
        # survive idle. agent_id doubles as the addressable tmux session name.
        self._agents: dict[str, dict] = {}
        # {agent_id: {"state": str, "tool": str, "in_plan_mode": bool}} — the
        # CURRENT busy state; absent == idle (matching the bridge's semantics
        # where "" is the meaningful idle value downstream).
        self._activities: dict[str, dict] = {}
        self._subscribers: set[asyncio.StreamWriter] = set()
        # Emit coalescing state (see _schedule_emit).
        self._emit_handle: asyncio.TimerHandle | None = None
        self._emit_dirty = False
        # Reconciler throttle + one-shot missing-CLI warning latch.
        self._last_claude_reconcile = 0.0
        self._claude_cli_warned = False

    # ── ingest ──────────────────────────────────────────────────────────

    def handle_hook_event(self, event: dict) -> None:
        """Fold one reporter payload into the table via the shared machine."""
        agent_id = str(event.get("agent_id", ""))
        if not agent_id:
            return
        hook_event = str(event.get("hook_event_name", ""))
        outcome = self._machine.apply(event)

        # SessionEnd removes the agent entirely — it is gone, not idle.
        if hook_event == "SessionEnd":
            if self._forget(agent_id):
                self._schedule_emit()
            return

        row = self._agents.get(agent_id)
        if row is None:
            row = {
                "project": "",
                "spawned_at": int(time.time()),
                "session_id": "",
                "last_event_mono": time.monotonic(),
            }
            self._agents[agent_id] = row
            log.info("agent registered | %s (event=%s)", agent_id, hook_event or "?")
        row["last_event_mono"] = time.monotonic()
        # Project follows the agent's cwd (it can change over a session's life).
        cwd = str(event.get("cwd", "") or "")
        if cwd:
            row["project"] = os.path.basename(cwd.rstrip("/")) or cwd
        # Sticky session id (survives idle — needed by the reconciler and any
        # future session-restore consumer). Overwrite only when present.
        if outcome.session_id:
            row["session_id"] = outcome.session_id

        prev_state = self._activities.get(agent_id, {}).get("state", "")
        if outcome.clear:
            self._activities.pop(agent_id, None)
        elif outcome.state:
            self._activities[agent_id] = {
                "state": outcome.state,
                "tool": outcome.tool,
                "in_plan_mode": outcome.in_plan_mode,
            }
        new_state = self._activities.get(agent_id, {}).get("state", "")
        if prev_state != new_state:
            log.info(
                "activity | %s %s→%s event=%s tool=%s",
                agent_id,
                prev_state or "-",
                new_state or "-",
                hook_event or "?",
                outcome.tool,
            )
        self._schedule_emit()

    def _forget(self, agent_id: str) -> bool:
        """Drop every trace of an agent; True when a row was actually removed."""
        removed = self._agents.pop(agent_id, None) is not None
        if removed:
            log.info("agent forgotten | %s", agent_id)
        self._activities.pop(agent_id, None)
        self._machine.forget(agent_id)
        return removed

    # ── snapshot / subscribers ──────────────────────────────────────────

    def snapshot_line(self) -> str:
        """The consolidated JSON line (bridge-compatible subset schema)."""
        agents = []
        projects = set()
        for agent_id, row in self._agents.items():
            project = row["project"] or "unknown"
            projects.add(project)
            activity = self._activities.get(agent_id, {})
            agents.append(
                {
                    "id": agent_id,
                    "project": project,
                    "title": agent_id,
                    "active": True,
                    "remote": False,
                    "spawned_at": row["spawned_at"],
                    "agent_type": "claude",
                    "activity_state": activity.get("state", ""),
                    "activity_tool": activity.get("tool", ""),
                    "in_plan_mode": activity.get("in_plan_mode", False),
                    "session_id": row["session_id"],
                    # agent_id IS the tmux session name — that identity is what
                    # makes the agent attachable (ttyd `?arg=`, send-keys target).
                    "tmux_session": agent_id,
                    # Conversation context is not captured headless (yet); emit the
                    # empty shapes so consumers of the bridge schema need no guards.
                    "last_prompt": "",
                    "last_messages": [],
                }
            )
        agents.sort(key=lambda a: (a["project"], a["spawned_at"]))
        return json.dumps({"agents": agents, "projects": sorted(projects)})

    def add_subscriber(self, writer: asyncio.StreamWriter) -> None:
        self._subscribers.add(writer)
        log.info("subscribe | %d subscriber(s)", len(self._subscribers))
        try:
            writer.write((self.snapshot_line() + "\n").encode())
        except Exception as e:  # noqa: BLE001 — dead peer, drop it
            log.warning("subscribe | initial snapshot failed (%s)", e)
            self._subscribers.discard(writer)

    def remove_subscriber(self, writer: asyncio.StreamWriter) -> None:
        self._subscribers.discard(writer)

    def _emit(self) -> None:
        """Push the current snapshot to every subscriber (fire-and-forget)."""
        if not self._subscribers:
            return
        data = (self.snapshot_line() + "\n").encode()
        for w in list(self._subscribers):
            if w.is_closing():
                self._subscribers.discard(w)
                continue
            try:
                w.write(data)
            except Exception as e:  # noqa: BLE001 — any write failure = dead peer
                log.warning("emit | dropping dead subscriber (%s)", e)
                self._subscribers.discard(w)

    def _schedule_emit(self) -> None:
        """Leading-edge emit + one trailing edge after the coalesce window."""
        if self._emit_handle is None:
            self._emit()
            self._emit_dirty = False
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # no loop (unit tests): every emit is a leading edge
            self._emit_handle = loop.call_later(
                EMIT_COALESCE_SECONDS, self._end_coalesce
            )
        else:
            self._emit_dirty = True

    def _end_coalesce(self) -> None:
        self._emit_handle = None
        if self._emit_dirty:
            self._emit_dirty = False
            self._emit()

    # ── liveness: Esc-cancel reconciliation ─────────────────────────────

    async def reconcile_claude_sessions(self) -> None:
        """Force-clear busy agents whose session the claude CLI reports idle.

        Port of the shell bridge's reconciler (its docstring has the full
        rationale). Conservative on purpose: only an explicit CLI "idle"
        clears; a missing session or missing CLI leaves state untouched.
        """
        now = time.monotonic()
        if now - self._last_claude_reconcile < CLAUDE_RECONCILE_MIN_INTERVAL:
            return
        candidates = {
            aid: self._agents[aid]["session_id"]
            for aid in self._activities
            if aid in self._agents
            and self._agents[aid]["session_id"]
            and now - self._agents[aid]["last_event_mono"]
            >= CLAUDE_RECONCILE_AFTER_SECONDS
        }
        if not candidates:
            return

        self._last_claude_reconcile = now
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude",
                "agents",
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=CLAUDE_CLI_TIMEOUT_SECONDS
            )
            sessions = json.loads(out.decode("utf-8", errors="replace") or "[]")
        except FileNotFoundError:
            if not self._claude_cli_warned:
                self._claude_cli_warned = True
                log.warning(
                    "reconcile | claude CLI not found — "
                    "cancellation reconciliation disabled"
                )
            return
        except asyncio.TimeoutError:
            log.warning(
                "reconcile | claude agents --json timed out after %ds",
                CLAUDE_CLI_TIMEOUT_SECONDS,
            )
            return
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("reconcile | claude agents --json failed: %s", exc)
            return
        finally:
            # Reap on every abnormal exit so a timeout/cancel can't leak a zombie.
            if proc is not None and proc.returncode is None:
                proc.kill()
                try:
                    await proc.wait()
                except Exception:  # noqa: BLE001 — reaping is best-effort
                    pass

        # Last-wins on a duplicate sessionId is fine: ids are unique in
        # `claude agents --json` (one row per live session).
        status_by_session = {
            s.get("sessionId", ""): s.get("status", "")
            for s in sessions
            if isinstance(s, dict)
        }
        changed = False
        now_after_cli = time.monotonic()
        for aid, sid in candidates.items():
            if status_by_session.get(sid) != "idle":
                continue
            # Re-validate after the await: the CLI can take up to 10s, and a
            # hook arriving meanwhile may have resumed this agent — clearing
            # from the stale snapshot would blank a genuinely working agent.
            row = self._agents.get(aid)
            if row is None or aid not in self._activities:
                continue
            if now_after_cli - row["last_event_mono"] < CLAUDE_RECONCILE_AFTER_SECONDS:
                continue
            stale = self._activities.get(aid, {}).get("state", "?")
            log.info(
                "RECONCILE | %s session=%s CLI reports idle — clearing "
                "stuck '%s' (cancellation fired no hook)",
                aid,
                sid[:8],
                stale,
            )
            self._activities.pop(aid, None)
            # A finished turn's leftover SubagentStart depth is orphaned too.
            self._machine.forget(aid)
            changed = True
        if changed:
            self._schedule_emit()

    # ── liveness: dead tmux session pruning ─────────────────────────────

    async def prune_dead_tmux_sessions(self) -> None:
        """Drop agents whose tmux session (their id) no longer exists.

        A killed session fires no SessionEnd, so without this a dead agent
        would linger in the table forever. Only runs when a tmux socket was
        configured. A non-running tmux server means NO sessions exist, so the
        whole table is cleared in that case — but ONLY on tmux's explicit
        "no server running" error; any other failure (bad socket path,
        permissions) leaves the table untouched rather than mass-forgetting
        live agents on operator error.
        """
        if not self._tmux_socket or not self._agents:
            return
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux",
                "-S",
                self._tmux_socket,
                "list-sessions",
                "-F",
                "#{session_name}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=5)
        except FileNotFoundError:
            return  # no tmux binary — nothing to prune against
        except asyncio.TimeoutError:
            log.warning("prune | tmux list-sessions timed out")
            return
        except OSError as exc:
            log.warning("prune | tmux list-sessions failed to spawn: %s", exc)
            return
        finally:
            if proc is not None and proc.returncode is None:
                proc.kill()
                try:
                    await proc.wait()
                except Exception:  # noqa: BLE001
                    pass
        if proc.returncode != 0 and b"no server running" not in err:
            log.warning(
                "prune | tmux list-sessions failed (rc=%s): %s",
                proc.returncode,
                err.decode("utf-8", errors="replace").strip(),
            )
            return
        live = {
            line.strip()
            for line in out.decode("utf-8", errors="replace").splitlines()
            if line.strip()
        }
        dead = [aid for aid in self._agents if aid not in live]
        if not dead:
            return
        for aid in dead:
            log.info("prune | tmux session gone — dropping %s", aid)
            self._forget(aid)
        self._schedule_emit()


# ── wire protocol ────────────────────────────────────────────────────────

# One JSON line can carry a hook payload; keep a generous cap against garbage.
_MAX_LINE_BYTES = 256 * 1024


async def _handle_client(
    hub: HeadlessAgentHub, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Serve one connection: hook reporters (one line, gone) or subscribers."""
    subscribed = False
    try:
        while True:
            try:
                line = await reader.readline()
            except (ValueError, ConnectionResetError):
                break  # oversized line / peer reset
            if not line:
                break
            line = line.strip()
            if not line or len(line) > _MAX_LINE_BYTES:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.warning("client | bad json (%.80s…)", line.decode(errors="replace"))
                continue
            if not isinstance(msg, dict):
                continue
            msg_type = msg.get("type")
            if msg_type == "hook":
                hub.handle_hook_event(msg)
            elif msg_type == "subscribe":
                if not subscribed:
                    subscribed = True
                    hub.add_subscriber(writer)
            else:
                log.debug("client | ignoring message type %r", msg_type)
    finally:
        if subscribed:
            hub.remove_subscriber(writer)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 — peer already gone
            pass


async def _reaper(hub: HeadlessAgentHub) -> None:
    """Periodic liveness pass: Esc-cancel reconcile + dead-session prune."""
    while True:
        await asyncio.sleep(REAPER_INTERVAL_SECONDS)
        try:
            await hub.reconcile_claude_sessions()
            await hub.prune_dead_tmux_sessions()
        except Exception:  # noqa: BLE001 — the reaper must survive anything
            log.exception("reaper | pass failed")


async def _serve(socket_path: str, tmux_socket: str) -> None:
    hub = HeadlessAgentHub(tmux_socket=tmux_socket)
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A stale socket file from a previous run blocks bind; the service manager
    # (systemd) guarantees single-instance, so unlinking is safe here.
    path.unlink(missing_ok=True)
    # limit= lifts the StreamReader's default 64 KiB readline cap — without it
    # the _MAX_LINE_BYTES skip guard in _handle_client is unreachable (readline
    # raises at 64 KiB and drops the whole connection instead).
    server = await asyncio.start_unix_server(
        lambda r, w: _handle_client(hub, r, w),
        path=str(path),
        limit=_MAX_LINE_BYTES,
    )
    log.info(
        "agent hub listening on %s (tmux prune: %s)", socket_path, tmux_socket or "off"
    )
    reaper = asyncio.ensure_future(_reaper(hub))
    try:
        async with server:
            await server.serve_forever()
    finally:
        reaper.cancel()
        path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--socket",
        default=os.environ.get(
            "VIGILIA_AGENT_HUB_SOCK",
            str(Path.home() / ".vigilia" / "agent-hub.sock"),
        ),
        help="Unix socket to listen on (env: VIGILIA_AGENT_HUB_SOCK)",
    )
    parser.add_argument(
        "--tmux-socket",
        default=os.environ.get("VIGILIA_TMUX_SOCKET", ""),
        help="tmux server socket to prune dead agent sessions against "
        "(empty = pruning off; env: VIGILIA_TMUX_SOCKET)",
    )
    parser.add_argument(
        "--log-level", default="INFO", help="logging level (DEBUG/INFO/WARNING/ERROR)"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        asyncio.run(_serve(args.socket, args.tmux_socket))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
