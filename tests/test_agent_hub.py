"""Unit tests for the headless agent hub (table + wire protocol).

The event→state semantics themselves are pinned by test_agent_activity.py —
these tests cover what the hub adds on top: the hook-driven agent table,
the bridge-compatible snapshot schema, SessionEnd/prune forgetting, and the
subscribe protocol over a real Unix socket.
"""

from __future__ import annotations

import asyncio
import json

from symmetria_ide.agent_hub import HeadlessAgentHub, _handle_client


def _event(hook_event: str, *, agent_id: str = "vigilia-abc12-1", **fields) -> dict:
    """A reporter payload as vigilia-agent-hook.py forwards it."""
    return {
        "type": "hook",
        "agent_id": agent_id,
        "hook_event_name": hook_event,
        "cwd": "/opt/dev/repos/vigilia",
        **fields,
    }


def _snapshot(hub: HeadlessAgentHub) -> dict:
    return json.loads(hub.snapshot_line())


# ---------------------------------------------------------------------------
# Agent table lifecycle
# ---------------------------------------------------------------------------


def test_first_event_registers_agent_with_project_from_cwd():
    hub = HeadlessAgentHub()
    hub.handle_hook_event(_event("SessionStart"))
    snap = _snapshot(hub)
    assert snap["projects"] == ["vigilia"]
    (agent,) = snap["agents"]
    assert agent["id"] == "vigilia-abc12-1"
    assert agent["project"] == "vigilia"
    assert agent["tmux_session"] == "vigilia-abc12-1"
    assert agent["agent_type"] == "claude"
    assert agent["active"] is True
    assert agent["activity_state"] == "starting"


def test_activity_transitions_flow_through_the_shared_machine():
    hub = HeadlessAgentHub()
    hub.handle_hook_event(_event("UserPromptSubmit"))
    assert _snapshot(hub)["agents"][0]["activity_state"] == "thinking"
    hub.handle_hook_event(_event("PreToolUse", tool_name="Bash"))
    agent = _snapshot(hub)["agents"][0]
    assert agent["activity_state"] == "working"
    assert agent["activity_tool"] == "Running"
    hub.handle_hook_event(_event("Stop"))
    agent = _snapshot(hub)["agents"][0]
    # Idle clears activity but the agent row SURVIVES (still attachable).
    assert agent["activity_state"] == ""
    assert agent["activity_tool"] == ""


def test_session_end_forgets_the_agent_entirely():
    hub = HeadlessAgentHub()
    hub.handle_hook_event(_event("SessionStart"))
    hub.handle_hook_event(_event("SessionEnd"))
    snap = _snapshot(hub)
    assert snap["agents"] == []
    assert snap["projects"] == []


def test_recap_subagent_stop_does_not_resurrect_idle():
    hub = HeadlessAgentHub()
    hub.handle_hook_event(_event("UserPromptSubmit"))
    hub.handle_hook_event(_event("Stop"))
    # The recap echo: SubagentStop with no SubagentStart this turn.
    hub.handle_hook_event(_event("SubagentStop"))
    assert _snapshot(hub)["agents"][0]["activity_state"] == ""


def test_plan_mode_reflected_in_snapshot():
    hub = HeadlessAgentHub()
    hub.handle_hook_event(_event("UserPromptSubmit", permission_mode="plan"))
    assert _snapshot(hub)["agents"][0]["in_plan_mode"] is True


def test_session_id_is_sticky_across_idle():
    hub = HeadlessAgentHub()
    hub.handle_hook_event(_event("UserPromptSubmit", session_id="abc-123"))
    hub.handle_hook_event(_event("Stop"))
    assert _snapshot(hub)["agents"][0]["session_id"] == "abc-123"


def test_event_without_agent_id_is_ignored():
    hub = HeadlessAgentHub()
    hub.handle_hook_event({"type": "hook", "hook_event_name": "SessionStart"})
    assert _snapshot(hub)["agents"] == []


def test_agents_grouped_and_sorted_by_project():
    hub = HeadlessAgentHub()
    hub.handle_hook_event(_event("SessionStart", agent_id="zeta-1", cwd="/repos/zeta"))
    hub.handle_hook_event(
        _event("SessionStart", agent_id="alpha-1", cwd="/repos/alpha")
    )
    snap = _snapshot(hub)
    assert snap["projects"] == ["alpha", "zeta"]
    assert [a["project"] for a in snap["agents"]] == ["alpha", "zeta"]


# ---------------------------------------------------------------------------
# Snapshot schema parity (the fields vigiliad's hubAgent decodes)
# ---------------------------------------------------------------------------


def test_snapshot_carries_the_bridge_schema_fields():
    hub = HeadlessAgentHub()
    hub.handle_hook_event(_event("SessionStart"))
    (agent,) = _snapshot(hub)["agents"]
    for field in (
        "id",
        "project",
        "title",
        "active",
        "remote",
        "spawned_at",
        "agent_type",
        "activity_state",
        "activity_tool",
        "in_plan_mode",
        "session_id",
        "tmux_session",
        "last_prompt",
        "last_messages",
    ):
        assert field in agent, f"snapshot missing bridge field {field!r}"
    assert agent["remote"] is False
    assert isinstance(agent["spawned_at"], int)


# ---------------------------------------------------------------------------
# Wire protocol (real Unix socket round-trip)
# ---------------------------------------------------------------------------


def test_subscribe_receives_snapshot_then_updates(tmp_path):
    sock = tmp_path / "hub.sock"

    async def scenario() -> list[dict]:
        hub = HeadlessAgentHub()
        server = await asyncio.start_unix_server(
            lambda r, w: _handle_client(hub, r, w), path=str(sock)
        )
        async with server:
            reader, writer = await asyncio.open_unix_connection(str(sock))
            writer.write(b'{"type":"subscribe"}\n')
            await writer.drain()
            initial = json.loads(await asyncio.wait_for(reader.readline(), 2))

            # A hook reporter connects, fires one event, disconnects.
            _, hook_writer = await asyncio.open_unix_connection(str(sock))
            hook_writer.write((json.dumps(_event("UserPromptSubmit")) + "\n").encode())
            await hook_writer.drain()
            hook_writer.close()
            await hook_writer.wait_closed()

            update = json.loads(await asyncio.wait_for(reader.readline(), 2))
            writer.close()
            await writer.wait_closed()
            return [initial, update]

    initial, update = asyncio.run(scenario())
    assert initial == {"agents": [], "projects": []}
    assert update["agents"][0]["activity_state"] == "thinking"


def test_garbage_lines_do_not_kill_the_connection(tmp_path):
    sock = tmp_path / "hub.sock"

    async def scenario() -> dict:
        hub = HeadlessAgentHub()
        server = await asyncio.start_unix_server(
            lambda r, w: _handle_client(hub, r, w), path=str(sock)
        )
        async with server:
            _, writer = await asyncio.open_unix_connection(str(sock))
            writer.write(b"not json at all\n")
            writer.write(b"[1, 2, 3]\n")  # valid JSON, not an object
            writer.write((json.dumps(_event("SessionStart")) + "\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.05)  # let the server task drain the lines
            return json.loads(hub.snapshot_line())

    snap = asyncio.run(scenario())
    assert len(snap["agents"]) == 1


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------


def test_prune_drops_agents_whose_tmux_session_died(monkeypatch):
    hub = HeadlessAgentHub(tmux_socket="/tmp/fake-tmux.sock")
    hub.handle_hook_event(_event("SessionStart", agent_id="alive-1"))
    hub.handle_hook_event(_event("SessionStart", agent_id="dead-1"))

    async def fake_exec(*argv, **kwargs):
        class FakeProc:
            returncode = 0

            async def communicate(self):
                return b"alive-1\n", b""

        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(hub.prune_dead_tmux_sessions())
    snap = json.loads(hub.snapshot_line())
    assert [a["id"] for a in snap["agents"]] == ["alive-1"]


def test_reconcile_clears_agent_the_cli_reports_idle(monkeypatch):
    hub = HeadlessAgentHub()
    hub.handle_hook_event(_event("UserPromptSubmit", session_id="sess-1"))
    # Make the agent look hook-silent for longer than the reconcile threshold.
    hub._agents["vigilia-abc12-1"]["last_event_mono"] -= 60

    async def fake_exec(*argv, **kwargs):
        class FakeProc:
            returncode = 0

            async def communicate(self):
                return json.dumps(
                    [{"sessionId": "sess-1", "status": "idle"}]
                ).encode(), b""

        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(hub.reconcile_claude_sessions())
    assert json.loads(hub.snapshot_line())["agents"][0]["activity_state"] == ""


def test_reconcile_leaves_busy_session_untouched(monkeypatch):
    hub = HeadlessAgentHub()
    hub.handle_hook_event(_event("PreToolUse", tool_name="Bash", session_id="sess-1"))
    hub._agents["vigilia-abc12-1"]["last_event_mono"] -= 60

    async def fake_exec(*argv, **kwargs):
        class FakeProc:
            returncode = 0

            async def communicate(self):
                return json.dumps(
                    [{"sessionId": "sess-1", "status": "busy"}]
                ).encode(), b""

        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(hub.reconcile_claude_sessions())
    assert json.loads(hub.snapshot_line())["agents"][0]["activity_state"] == "working"


class _FakeWriter:
    """Minimal StreamWriter stand-in that records emitted snapshot lines."""

    def __init__(self):
        self.lines: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.lines.append(data)

    def is_closing(self) -> bool:
        return False


def test_event_burst_coalesces_to_leading_plus_trailing_emit():
    async def scenario() -> int:
        hub = HeadlessAgentHub()
        writer = _FakeWriter()
        hub.add_subscriber(writer)  # writes 1 initial snapshot
        for event_name in ("SessionStart", "UserPromptSubmit", "PreToolUse"):
            hub.handle_hook_event(_event(event_name, tool_name="Bash"))
        await asyncio.sleep(0.2)  # let the trailing coalesce edge fire
        return len(writer.lines)

    # 1 initial + leading edge + one trailing edge for the burst = 3 total.
    assert asyncio.run(scenario()) == 3


def test_reconcile_min_interval_throttles_cli_invocations(monkeypatch):
    hub = HeadlessAgentHub()
    hub.handle_hook_event(_event("UserPromptSubmit", session_id="sess-1"))
    hub._agents["vigilia-abc12-1"]["last_event_mono"] -= 60
    calls = []

    async def fake_exec(*argv, **kwargs):
        calls.append(argv)

        class FakeProc:
            returncode = 0

            async def communicate(self):
                return b"[]", b""

        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def scenario():
        await hub.reconcile_claude_sessions()
        await hub.reconcile_claude_sessions()  # within min interval → no-op

    asyncio.run(scenario())
    assert len(calls) == 1


def test_reconcile_missing_cli_warns_once_and_leaves_state(monkeypatch):
    hub = HeadlessAgentHub()
    hub.handle_hook_event(_event("PreToolUse", tool_name="Bash", session_id="s1"))
    hub._agents["vigilia-abc12-1"]["last_event_mono"] -= 60

    async def fake_exec(*argv, **kwargs):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(hub.reconcile_claude_sessions())
    assert hub._claude_cli_warned is True
    assert json.loads(hub.snapshot_line())["agents"][0]["activity_state"] == "working"


def test_reconcile_revalidates_after_cli_await(monkeypatch):
    """A hook arriving DURING the CLI await must veto the stale idle verdict."""
    hub = HeadlessAgentHub()
    hub.handle_hook_event(_event("UserPromptSubmit", session_id="sess-1"))
    hub._agents["vigilia-abc12-1"]["last_event_mono"] -= 60

    async def fake_exec(*argv, **kwargs):
        class FakeProc:
            returncode = 0

            async def communicate(self):
                # The agent resumes work while the CLI is running: the fresh
                # event refreshes last_event_mono, so the idle verdict below
                # is stale and must not clear the new activity.
                hub.handle_hook_event(
                    _event("PreToolUse", tool_name="Bash", session_id="sess-1")
                )
                return (
                    json.dumps([{"sessionId": "sess-1", "status": "idle"}]).encode(),
                    b"",
                )

        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(hub.reconcile_claude_sessions())
    assert json.loads(hub.snapshot_line())["agents"][0]["activity_state"] == "working"


def test_prune_leaves_table_on_tmux_failure(monkeypatch):
    """A failing tmux (bad socket, permissions) must NOT mass-forget agents."""
    hub = HeadlessAgentHub(tmux_socket="/tmp/wrong.sock")
    hub.handle_hook_event(_event("SessionStart", agent_id="alive-1"))

    async def fake_exec(*argv, **kwargs):
        class FakeProc:
            returncode = 1

            async def communicate(self):
                return b"", b"error connecting to /tmp/wrong.sock (No such file)"

        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(hub.prune_dead_tmux_sessions())
    assert len(json.loads(hub.snapshot_line())["agents"]) == 1


def test_prune_clears_all_when_tmux_server_is_gone(monkeypatch):
    """tmux's explicit no-server error means zero live sessions → clear all."""
    hub = HeadlessAgentHub(tmux_socket="/tmp/gone.sock")
    hub.handle_hook_event(_event("SessionStart", agent_id="dead-1"))

    async def fake_exec(*argv, **kwargs):
        class FakeProc:
            returncode = 1

            async def communicate(self):
                return b"", b"no server running on /tmp/gone.sock"

        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(hub.prune_dead_tmux_sessions())
    assert json.loads(hub.snapshot_line())["agents"] == []
