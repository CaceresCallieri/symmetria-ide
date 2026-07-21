"""Terminal-agent pool tests (the IDE-native orchestrator runtime).

Hermetic: no bridge socket, no QML, no subprocesses. The real
AgentBridgeClient is constructed but never started; publish assertions go
through a fake swapped into `controller._agent_bridge` after construction
(`_on_bridge_snapshot` is exercised by direct invocation, mirroring the
queued delivery path).
"""

from __future__ import annotations

import concurrent.futures
import errno
import io
import os
import re
import shutil
import time

import pytest

from symmetria_ide.agent_harness import CLAUDE_ENV_UNSET_ARGS
from symmetria_ide.app import AppController, _agent_tmux_socket, _existing_root
from symmetria_ide.worktree import linked_worktree_info


class FakeBridge:
    """Captures the publish API surface of AgentBridgeClient."""

    def __init__(self) -> None:
        self.spawns: list[dict] = []
        self.removes: list[int] = []
        self.focuses: list[int] = []
        self.titles: list[tuple[int, str]] = []
        self.activities: list[dict] = []
        self.start_calls = 0
        self.stop_calls = 0

    def notify_spawn(self, instance: dict) -> None:
        self.spawns.append(instance)

    def notify_remove(self, slot: int) -> None:
        self.removes.append(slot)

    def notify_focus(self, slot: int) -> None:
        self.focuses.append(slot)

    def notify_title(self, slot: int, title: str) -> None:
        self.titles.append((slot, title))

    def notify_activity(
        self,
        slot: int,
        *,
        state: str,
        tool: str,
        in_plan_mode: bool,
        session_id: str = "",
    ) -> None:
        self.activities.append(
            {
                "slot": slot,
                "state": state,
                "tool": tool,
                "in_plan_mode": in_plan_mode,
                "session_id": session_id,
            }
        )

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


@pytest.fixture
def controller(monkeypatch):
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_PROMPT", raising=False)
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_VIEW", raising=False)
    # spawn_agent guards on `claude` being installed — fake it present so
    # the tests don't depend on the host having the CLI.
    monkeypatch.setattr(
        "symmetria_ide.app.shutil.which", lambda _name: "/usr/bin/claude"
    )
    c = AppController()
    c._agent_bridge = FakeBridge()
    return c


@pytest.fixture
def bridge(controller) -> FakeBridge:
    return controller._agent_bridge


# ---------------------------------------------------------------------------
# Spawn: argv construction + dangerous polarity
# ---------------------------------------------------------------------------


def test_spawn_fresh_dangerous_default_argv(controller):
    controller.spawn_agent("fresh", True)
    argv = controller.agent_spawn_argv(1)
    # The env wrapper now also exports the IDE agent-socket path, and claude
    # gets the IDE reporter hook injected via --settings (agent-ownership
    # inversion). Both reference the controller's own state so the assertion
    # tracks the real socket path / settings string.
    assert argv == [
        "env",
        *CLAUDE_ENV_UNSET_ARGS,
        f"SYMMETRIA_AGENT_ID={os.getpid()}_1",
        f"SYMMETRIA_IDE_AGENT_SOCK={controller._agent_events.socket_path}",
        "SYMMETRIA_IDE_STATUSLINE_TAP=1",
        "claude",
        "--no-chrome",
        "--dangerously-skip-permissions",
        "--settings",
        controller._agent_reporter_settings,
    ]


def test_spawn_safe_variant_omits_dangerous_flag(controller):
    controller.spawn_agent("fresh", False)
    assert "--dangerously-skip-permissions" not in controller.agent_spawn_argv(1)


def test_spawn_resume_appends_r_flag(controller):
    controller.spawn_agent("resume", True)
    assert controller.agent_spawn_argv(1)[-1] == "-r"


def test_spawn_continue_appends_c_flag(controller):
    controller.spawn_agent("continue", False)
    assert controller.agent_spawn_argv(1)[-1] == "-c"


def test_spawn_unknown_type_is_a_no_op(controller):
    controller.spawn_agent("bogus", True)
    assert controller.agentOrder == []


def test_spawn_without_claude_on_path_is_a_no_op(controller, monkeypatch):
    monkeypatch.setattr("symmetria_ide.app.shutil.which", lambda _name: None)
    controller.spawn_agent("fresh", True)
    assert controller.agentOrder == []


def test_agent_spawn_argv_for_empty_slot_returns_empty(controller):
    assert controller.agent_spawn_argv(3) == []


# ---------------------------------------------------------------------------
# Harness selection (claude / opencode)
# ---------------------------------------------------------------------------


def test_spawn_opencode_fresh_dangerous_argv(controller):
    controller.spawn_agent("fresh", True, "opencode")
    # opencode exports the IDE agent-socket env uniformly but gets NO --settings
    # (no settings_flag) — its agents keep reporting to the shell bridge.
    assert controller.agent_spawn_argv(1) == [
        "env",
        *CLAUDE_ENV_UNSET_ARGS,
        f"SYMMETRIA_AGENT_ID={os.getpid()}_1",
        f"SYMMETRIA_IDE_AGENT_SOCK={controller._agent_events.socket_path}",
        "SYMMETRIA_IDE_STATUSLINE_TAP=1",
        'OPENCODE_PERMISSION={"*":{"*":"allow"}}',
        "opencode",
    ]
    assert "--settings" not in controller.agent_spawn_argv(1)


def test_spawn_opencode_resume_with_session_id_argv(controller):
    controller.spawn_agent("resume", False, "opencode", "ses_abc")
    assert controller.agent_spawn_argv(1)[-2:] == ["--session", "ses_abc"]


def test_spawn_opencode_resume_without_session_id_is_a_no_op(controller):
    # `opencode --session` with no id errors on spawn — the controller
    # refuses the spawn instead (the QML picker supplies the id).
    controller.spawn_agent("resume", True, "opencode")
    assert controller.agentOrder == []


# ---------------------------------------------------------------------------
# tmux substrate (SYMMETRIA_IDE_AGENT_TMUX=1) — opt-in wrap + close-kill
# ---------------------------------------------------------------------------


def test_agent_spawn_argv_no_tmux_wrap_by_default(controller):
    # Default (flag unset): the argv is the bare env-wrapper, no tmux — the
    # substrate must be strictly opt-in so it can't surprise the daily driver.
    controller.spawn_agent("fresh", True)
    argv = controller.agent_spawn_argv(1)
    assert argv[0] == "env"
    assert "tmux" not in argv


def test_agent_spawn_argv_wraps_in_tmux_when_enabled(controller, monkeypatch, tmp_path):
    sock = str(tmp_path / "t.sock")
    monkeypatch.setenv("SYMMETRIA_IDE_AGENT_TMUX", "1")
    monkeypatch.setenv("SYMMETRIA_IDE_TMUX_SOCKET", sock)
    controller.spawn_agent("fresh", True)
    argv = controller.agent_spawn_argv(1)
    assert argv[0] == "tmux"
    assert argv[1:3] == ["-S", sock]
    assert "-f" in argv  # the agent-tmux.conf server flag
    assert "new-session" in argv
    assert "-A" in argv  # attach-if-exists
    assert "-c" in argv  # start-directory → correct cwd attribution
    s = argv.index("-s")
    assert argv[s + 1].endswith("-1")  # <project-slug>-<slot> session name
    # The inner env-wrapper rides after the session name, contiguous and intact.
    assert "env" in argv[s + 2 :]
    assert any(a == "claude" or a.endswith("/claude") for a in argv)


def test_agent_spawn_argv_kills_orphan_session_before_create(
    controller, monkeypatch, tmp_path
):
    # Spawn semantics win over silent re-adoption: before returning the
    # `new-session -A` wrap, agent_spawn_argv must kill any surviving orphan
    # with the slot's deterministic name — otherwise -A would ATTACH it and
    # the requested spawn type (fresh/resume/continue) would never run.
    sock = str(tmp_path / "t.sock")
    monkeypatch.setenv("SYMMETRIA_IDE_AGENT_TMUX", "1")
    monkeypatch.setenv("SYMMETRIA_IDE_TMUX_SOCKET", sock)
    calls: list = []
    monkeypatch.setattr(
        "symmetria_ide.app.subprocess.run", lambda *a, **k: calls.append(a[0])
    )
    controller.spawn_agent("resume", True)
    argv = controller.agent_spawn_argv(1)
    assert len(calls) == 1, "expected exactly one orphan kill at spawn"
    kill_argv = calls[0]
    assert "kill-session" in kill_argv
    assert "-S" in kill_argv and sock in kill_argv
    session_name = argv[argv.index("-s") + 1]
    assert kill_argv[-1] == session_name  # kills exactly the name -A would adopt


def test_agent_spawn_argv_no_orphan_kill_without_tmux(controller, monkeypatch):
    # Flag unset: direct-PTY spawns must not shell out to tmux at all.
    calls: list = []
    monkeypatch.setattr(
        "symmetria_ide.app.subprocess.run", lambda *a, **k: calls.append(a[0])
    )
    controller.spawn_agent("fresh", True)
    controller.agent_spawn_argv(1)
    assert calls == []


def test_close_agent_kills_tmux_session_when_wrapped(controller, monkeypatch, tmp_path):
    sock = str(tmp_path / "t.sock")
    monkeypatch.setenv("SYMMETRIA_IDE_AGENT_TMUX", "1")
    monkeypatch.setenv("SYMMETRIA_IDE_TMUX_SOCKET", sock)
    calls: list = []
    monkeypatch.setattr(
        "symmetria_ide.app.subprocess.run", lambda *a, **k: calls.append(a[0])
    )
    controller.spawn_agent("fresh", True)
    controller.agent_spawn_argv(1)  # records inst["tmux_socket"] — the wrap succeeded
    calls.clear()  # drop the spawn-time orphan kill; this test targets close
    controller.close_agent(1)
    assert calls, "expected a tmux kill-session on close"
    argv = calls[0]
    assert "kill-session" in argv
    assert "-S" in argv and sock in argv  # the socket recorded at spawn, not a re-read
    assert argv[-1].endswith("-1")  # the <project-slug>-<slot> session name


def test_close_agent_skips_tmux_kill_when_not_wrapped(controller, monkeypatch):
    # Flag unset: agent_spawn_argv never records a socket, so the kill helper
    # self-gates to a no-op — the legacy direct-PTY close path is untouched.
    calls: list = []
    monkeypatch.setattr(
        "symmetria_ide.app.subprocess.run", lambda *a, **k: calls.append(a[0])
    )
    controller.spawn_agent("fresh", True)
    controller.agent_spawn_argv(1)
    controller.close_agent(1)
    assert calls == []


def test_agent_spawn_argv_falls_back_to_direct_pty_when_socket_dir_unusable(
    controller, monkeypatch, tmp_path
):
    monkeypatch.setenv("SYMMETRIA_IDE_AGENT_TMUX", "1")
    monkeypatch.setenv("SYMMETRIA_IDE_TMUX_SOCKET", str(tmp_path / "sub" / "t.sock"))

    def boom(*_a, **_k):
        raise OSError("no dir")

    monkeypatch.setattr("symmetria_ide.app.os.makedirs", boom)
    controller.spawn_agent("fresh", True)
    argv = controller.agent_spawn_argv(1)
    assert argv[0] == "env"  # direct-PTY fallback, not wrapped in tmux
    assert "tmux" not in argv
    # No socket recorded → a later close won't attempt a spurious kill-session.
    assert "tmux_socket" not in controller._term_agents[1]


def test_shipped_agent_tmux_conf_exists():
    # Packaging / _RUNTIME_DIR relocation guard: the conf must resolve on disk, or
    # every tmux spawn silently loses the shared config (status bar reappears).
    from symmetria_ide.app import _AGENT_TMUX_CONF

    assert _AGENT_TMUX_CONF.exists(), f"shipped tmux conf missing at {_AGENT_TMUX_CONF}"


def test_spawn_unknown_harness_is_a_no_op(controller):
    controller.spawn_agent("fresh", True, "copilot")
    assert controller.agentOrder == []


def test_spawn_opencode_publishes_agent_type(controller, bridge):
    controller.spawn_agent("fresh", True, "opencode")
    assert bridge.spawns[0]["agent_type"] == "opencode"


def test_opencode_activity_fallback_uses_slot_harness(controller):
    # Pre-first-activity (no bridge snapshot yet) the chip must show
    # the harness we spawned, not default to claude.
    controller.spawn_agent("fresh", True, "opencode")
    assert controller.agentActivity[0]["agentType"] == "opencode"


class _FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fetch_payload(controller, monkeypatch, **run_result):
    """Run _fetch_opencode_sessions with a faked subprocess and capture
    the worker's emitted payload (same-thread emit → direct connection,
    so no event-loop pump is needed)."""
    captured: list[dict] = []
    controller._opencode_sessions_fetched.connect(captured.append)

    def fake_run(*_a, **_k):
        if "raises" in run_result:
            raise run_result["raises"]
        return _FakeCompletedProcess(**run_result)

    monkeypatch.setattr("symmetria_ide.app.subprocess.run", fake_run)
    controller._fetch_opencode_sessions("/tmp")
    assert len(captured) == 1
    return captured[0]


def test_fetch_opencode_sessions_success_payload(controller, monkeypatch):
    payload = _fetch_payload(
        controller, monkeypatch, stdout='[{"id": "ses_1", "title": "t"}]'
    )
    assert payload["ok"] is True
    assert [s["id"] for s in payload["sessions"]] == ["ses_1"]


def test_fetch_opencode_sessions_nonzero_exit_is_not_ok(controller, monkeypatch):
    payload = _fetch_payload(controller, monkeypatch, returncode=1, stderr="boom")
    assert payload == {"ok": False, "sessions": []}


def test_fetch_opencode_sessions_garbage_stdout_is_not_ok(controller, monkeypatch):
    payload = _fetch_payload(controller, monkeypatch, stdout="not json")
    assert payload == {"ok": False, "sessions": []}


def test_fetch_opencode_sessions_oserror_still_emits(controller, monkeypatch):
    # The emit is the contract — a dead worker would hang the picker in
    # its "loading" state, so every failure path must still report.
    payload = _fetch_payload(controller, monkeypatch, raises=OSError("no exe"))
    assert payload == {"ok": False, "sessions": []}


def test_snapshot_empty_agent_type_falls_back_to_slot_harness(controller):
    controller.spawn_agent("fresh", True, "opencode")
    controller._on_bridge_snapshot(
        {
            "agents": [
                {
                    "id": f"{os.getpid()}_1",
                    "activity_state": "working",
                    "activity_tool": "Running",
                    "agent_type": "",
                }
            ]
        }
    )
    assert controller.agentActivity[0]["agentType"] == "opencode"


# ---------------------------------------------------------------------------
# Slot allocation + pool-shape properties
# ---------------------------------------------------------------------------


def test_slots_fill_from_the_bottom(controller):
    controller.spawn_agent("fresh", True)
    controller.spawn_agent("fresh", True)
    assert controller.agentOrder == [1, 2]
    assert controller.agentSlotActive == [True, True, False, False, False]


def test_pool_exhaustion_at_max_slots(controller):
    for _ in range(controller.maxAgentSlots + 1):
        controller.spawn_agent("fresh", True)
    assert controller.agentOrder == [1, 2, 3, 4, 5]


def test_freed_internal_slot_is_reused(controller):
    controller.spawn_agent("fresh", True)
    controller.spawn_agent("fresh", True)
    controller.close_agent(1)
    controller.spawn_agent("fresh", True)
    # Internal slot 1 is reused (SYMMETRIA_AGENT_ID stays unique within
    # the 1..5 range) but the newcomer APPENDS in display order — the
    # survivor keeps position 1.
    assert sorted(controller.agentOrder) == [1, 2]
    assert controller.agentOrder == [2, 1]


def test_spawn_publishes_bridge_instance_payload(controller, bridge):
    controller.spawn_agent("fresh", True)
    assert len(bridge.spawns) == 1
    inst = bridge.spawns[0]
    assert inst["buf"] == 1
    assert inst["agent_type"] == "claude"
    assert inst["dangerous"] is True
    assert inst["color_idx"] == 1
    # When the suite runs from a LINKED worktree (e.g. the stable tree),
    # spawn_agent deliberately redirects the spawn to the MAIN checkout
    # (worktree follow, 2026-07-13), so the published project label is the
    # main root's basename — not displayedRoot's. In the primary checkout
    # (the dev tree) linked_worktree_info returns ("", "") and this reduces
    # to the plain displayedRoot expectation.
    main_root, _ = linked_worktree_info(controller.displayedRoot)
    assert inst["project"] == os.path.basename(main_root or controller.displayedRoot)
    # Flag off by default: no addressable tmux session is advertised. A non-tmux
    # agent has no standalone session, so the phone must not be told to open one.
    assert "tmux_session" not in inst


def test_spawn_payload_advertises_tmux_session_after_wrap(
    controller, bridge, monkeypatch, tmp_path
):
    # The addressable tmux name is advertised only once the wrap SUCCEEDS:
    # the spawn-time payload omits it (the wrap outcome isn't known yet), and
    # agent_spawn_argv re-publishes after setting tmux_socket. An external
    # control plane (vigiliad → the phone) uses it to attach ttyd / send-keys.
    monkeypatch.setenv("SYMMETRIA_IDE_AGENT_TMUX", "1")
    monkeypatch.setenv("SYMMETRIA_IDE_TMUX_SOCKET", str(tmp_path / "t.sock"))
    controller.spawn_agent("fresh", True)
    assert "tmux_session" not in bridge.spawns[0]  # pre-wrap: not advertised yet
    controller.agent_spawn_argv(1)  # runs the wrap → sets tmux_socket → re-notifies
    inst = bridge.spawns[-1]  # the re-published payload
    # <slug>-<6hex>-<slot>; the slug may itself contain hyphens (e.g.
    # "symmetria-ide"), so the leading class allows them — the 6-hex + slot
    # tail anchors the split.
    assert re.fullmatch(r"[a-z0-9-]+-[0-9a-f]{6}-\d+", inst["tmux_session"])
    assert inst["tmux_session"].endswith("-1")  # slot 1
    # The advertised value is EXACTLY the one used for the wrap and close-kill —
    # guards against a future refactor recomputing it divergently.
    assert inst["tmux_session"] == controller._term_agents[1]["tmux_session"]


def test_spawn_payload_omits_tmux_session_when_wrap_falls_back(
    controller, bridge, monkeypatch, tmp_path
):
    # If the wrap falls back to a direct PTY (socket dir unusable), the agent is
    # NOT in tmux — no published payload may advertise a phantom session name.
    monkeypatch.setenv("SYMMETRIA_IDE_AGENT_TMUX", "1")
    monkeypatch.setenv("SYMMETRIA_IDE_TMUX_SOCKET", str(tmp_path / "sub" / "t.sock"))

    def boom(*_a, **_k):
        raise OSError("socket dir unusable")

    monkeypatch.setattr("symmetria_ide.app.os.makedirs", boom)
    controller.spawn_agent("fresh", True)
    controller.agent_spawn_argv(1)  # wrap attempt → fallback to direct PTY
    assert all("tmux_session" not in payload for payload in bridge.spawns)


# ---------------------------------------------------------------------------
# Focus + surface auto-switch
# ---------------------------------------------------------------------------


def test_spawn_focuses_and_switches_to_agent_surface(controller, bridge):
    assert controller.centralSurface == "terminal"
    controller.spawn_agent("fresh", True)
    assert controller.focusedAgent == 1
    assert controller.centralSurface == "agent"
    assert controller.agentSurfaceVisible is True
    assert bridge.focuses == [1]


def test_focus_agent_on_empty_slot_is_a_no_op(controller, bridge):
    controller.focus_agent(4)
    assert controller.focusedAgent == 0
    assert controller.centralSurface == "terminal"
    assert bridge.focuses == []


def test_focus_agent_from_editor_switches_surface(controller):
    controller.spawn_agent("fresh", True)
    controller.swap_to_editor()
    controller.focus_agent(1)
    assert controller.centralSurface == "agent"


def test_cycle_agent_focus_wraps(controller):
    for _ in range(3):
        controller.spawn_agent("fresh", True)
    controller.focus_agent(3)
    controller.cycle_agent_focus(1)
    assert controller.focusedAgent == 1
    controller.cycle_agent_focus(-1)
    assert controller.focusedAgent == 3


def test_cycle_agent_focus_on_empty_pool_is_a_no_op(controller):
    controller.cycle_agent_focus(1)
    assert controller.focusedAgent == 0


# ---------------------------------------------------------------------------
# Close + refocus
# ---------------------------------------------------------------------------


def test_close_focused_refocuses_previous_in_display_order(controller):
    for _ in range(3):
        controller.spawn_agent("fresh", True)
    controller.focus_agent(3)
    controller.close_agent(3)
    assert controller.focusedAgent == 2
    assert controller.agentOrder == [1, 2]


def test_close_last_agent_falls_back_to_terminal_surface(controller, bridge):
    controller.spawn_agent("fresh", True)
    controller.close_focused_agent()
    assert controller.agentOrder == []
    assert controller.focusedAgent == 0
    assert controller.centralSurface == "terminal"
    assert bridge.removes == [1]


def test_close_unfocused_agent_keeps_focus(controller):
    controller.spawn_agent("fresh", True)
    controller.spawn_agent("fresh", True)
    controller.focus_agent(1)
    controller.close_agent(2)
    assert controller.focusedAgent == 1
    assert controller.centralSurface == "agent"


def test_on_agent_finished_closes_the_slot(controller, bridge):
    controller.spawn_agent("fresh", True)
    controller.on_agent_finished(1)
    assert controller.agentOrder == []
    assert bridge.removes == [1]


def test_on_agent_finished_after_close_is_idempotent(controller, bridge):
    controller.spawn_agent("fresh", True)
    controller.close_agent(1)
    controller.on_agent_finished(1)  # Loader teardown fires this — no double-publish
    assert bridge.removes == [1]


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------


def test_on_agent_title_updates_and_publishes(controller, bridge):
    controller.spawn_agent("fresh", True)
    controller.on_agent_title(1, "  fix the tests  ")
    assert controller.agentTitles[0] == "fix the tests"
    assert bridge.titles == [(1, "fix the tests")]


def test_on_agent_title_dedupes_unchanged_values(controller, bridge):
    controller.spawn_agent("fresh", True)
    controller.on_agent_title(1, "same")
    controller.on_agent_title(1, "same")
    assert bridge.titles == [(1, "same")]


def test_on_agent_title_for_empty_slot_is_dropped(controller, bridge):
    controller.on_agent_title(2, "ghost")
    assert bridge.titles == []


# ---------------------------------------------------------------------------
# Snapshot filtering → agentActivity
# ---------------------------------------------------------------------------


def _snapshot(*agents: dict) -> dict:
    return {"agents": list(agents), "projects": []}


def test_snapshot_mirrors_own_agents_activity(controller):
    controller.spawn_agent("fresh", True)
    controller._on_bridge_snapshot(
        _snapshot(
            {
                "id": f"{os.getpid()}_1",
                "activity_state": "working",
                "activity_tool": "Editing",
                "agent_type": "claude",
            }
        )
    )
    assert controller.agentActivity[0] == {
        "state": "working",
        "tool": "Editing",
        "agentType": "claude",
    }


def test_snapshot_ignores_foreign_pid_agents(controller):
    controller.spawn_agent("fresh", True)
    controller._on_bridge_snapshot(
        _snapshot({"id": "99999_1", "activity_state": "working"})
    )
    assert controller.agentActivity[0]["state"] == ""


def test_snapshot_ignores_unknown_slots(controller):
    controller.spawn_agent("fresh", True)
    controller._on_bridge_snapshot(
        _snapshot({"id": f"{os.getpid()}_4", "activity_state": "working"})
    )
    assert all(entry["state"] == "" for entry in controller.agentActivity)


def test_snapshot_clears_activity_when_agent_goes_quiet(controller):
    controller.spawn_agent("fresh", True)
    busy = _snapshot({"id": f"{os.getpid()}_1", "activity_state": "working"})
    controller._on_bridge_snapshot(busy)
    assert controller.agentActivity[0]["state"] == "working"
    controller._on_bridge_snapshot(_snapshot())
    assert controller.agentActivity[0]["state"] == ""


def test_snapshot_emits_only_on_change(controller):
    controller.spawn_agent("fresh", True)
    emissions: list[None] = []
    controller.agentActivityChanged.connect(lambda: emissions.append(None))
    payload = _snapshot({"id": f"{os.getpid()}_1", "activity_state": "working"})
    controller._on_bridge_snapshot(payload)
    controller._on_bridge_snapshot(payload)
    assert len(emissions) == 1


# ---------------------------------------------------------------------------
# Local capture (agent-ownership inversion): reporter hook → _on_agent_hook
# ---------------------------------------------------------------------------


def _hook(slot: int, hook_event: str, **fields) -> dict:
    """A reporter payload for the controller's agent at `slot`."""
    return {
        "type": "hook",
        "agent_id": f"{os.getpid()}_{slot}",
        "hook_event_name": hook_event,
        **fields,
    }


def test_local_hook_drives_activity(controller):
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(_hook(1, "PreToolUse", tool_name="Bash"))
    assert controller.agentActivity[0] == {
        "state": "working",
        "tool": "Running",
        "agentType": "claude",
    }


def test_local_hook_clears_activity_on_stop(controller):
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(_hook(1, "PreToolUse", tool_name="Bash"))
    assert controller.agentActivity[0]["state"] == "working"
    controller._on_agent_hook(_hook(1, "Stop"))
    assert controller.agentActivity[0]["state"] == ""


def test_local_hook_agent_type_from_slot_harness(controller):
    controller.spawn_agent("fresh", True, "opencode")
    # The reporter only runs for claude, but the activity dict's agentType must
    # reflect the slot's harness so an opencode chip never flashes the claude glyph.
    controller._on_agent_hook(_hook(1, "UserPromptSubmit"))
    assert controller.agentActivity[0]["agentType"] == "opencode"


def test_local_hook_ignores_foreign_pid(controller):
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(
        {"type": "hook", "agent_id": "99999_1", "hook_event_name": "PreToolUse"}
    )
    assert controller.agentActivity[0]["state"] == ""


def test_local_hook_ignores_closed_slot(controller):
    # No agent at slot 1 → the event is dropped without raising.
    controller._on_agent_hook(_hook(1, "PreToolUse", tool_name="Bash"))
    assert all(entry["state"] == "" for entry in controller.agentActivity)


def test_local_hook_emits_only_on_change(controller):
    controller.spawn_agent("fresh", True)
    emissions: list[None] = []
    controller.agentActivityChanged.connect(lambda: emissions.append(None))
    controller._on_agent_hook(_hook(1, "PreToolUse", tool_name="Bash"))
    controller._on_agent_hook(_hook(1, "PreToolUse", tool_name="Bash"))
    assert len(emissions) == 1


# -- session_id backfill --------------------------------------------------


def test_local_hook_backfills_session_id(controller):
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(_hook(1, "UserPromptSubmit", session_id="sess-abc"))
    assert controller._term_agents[1]["session_id"] == "sess-abc"


def test_local_session_id_is_sticky_across_clear(controller):
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(_hook(1, "UserPromptSubmit", session_id="sess-abc"))
    # A later Stop carrying no session id must NOT wipe the captured one.
    controller._on_agent_hook(_hook(1, "Stop"))
    assert controller._term_agents[1]["session_id"] == "sess-abc"


# -- local capture is authoritative over the bridge -----------------------


def test_local_capture_wins_over_bridge_snapshot(controller):
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(_hook(1, "PreToolUse", tool_name="Bash"))
    assert controller.agentActivity[0]["state"] == "working"
    # A lagging bridge snapshot for the SAME slot must not overwrite local state.
    controller._on_bridge_snapshot(
        _snapshot({"id": f"{os.getpid()}_1", "activity_state": "thinking"})
    )
    assert controller.agentActivity[0]["state"] == "working"


def test_bridge_snapshot_omitting_local_agent_does_not_wipe_it(controller):
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(_hook(1, "PreToolUse", tool_name="Bash"))
    # A snapshot that doesn't list our agent at all (race / bridge lag) must
    # leave the locally-captured sparkle intact — the seed-from-local rebuild.
    controller._on_bridge_snapshot(_snapshot())
    assert controller.agentActivity[0]["state"] == "working"


def test_bridge_drives_slot_until_first_local_report(controller):
    # Before any local report, the bridge path still fills the slot (Phase 1 is
    # additive — capture takes over only once the reporter has fired once).
    controller.spawn_agent("fresh", True)
    controller._on_bridge_snapshot(
        _snapshot({"id": f"{os.getpid()}_1", "activity_state": "thinking"})
    )
    assert controller.agentActivity[0]["state"] == "thinking"
    # The first local report claims the slot; the bridge can no longer change it.
    controller._on_agent_hook(_hook(1, "PreToolUse", tool_name="Bash"))
    controller._on_bridge_snapshot(
        _snapshot({"id": f"{os.getpid()}_1", "activity_state": "idle"})
    )
    assert controller.agentActivity[0]["state"] == "working"


def test_close_agent_releases_local_capture(controller):
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(_hook(1, "PreToolUse", tool_name="Bash"))
    assert 1 in controller._locally_captured_agents
    controller.close_agent(1)
    assert 1 not in controller._locally_captured_agents


def test_close_agent_forgets_machine_state_for_slot_reuse(controller):
    # close_agent must clear the activity machine's per-agent memory (subagent
    # depth), or a respawn into the same slot would inherit stale nesting.
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(_hook(1, "SubagentStart"))  # depth 0→1
    controller.close_agent(1)
    # Respawn into the freed slot; the prior SubagentStart must be forgotten, so
    # this stop is unpaired (recap drop) → no activity, not a "thinking".
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(_hook(1, "SubagentStop"))
    assert controller.agentActivity[0]["state"] == ""


# -- Phase 2: local capture publishes activity outward to the bridge ------


def test_local_hook_publishes_activity(controller, bridge):
    controller.spawn_agent("fresh", True)
    bridge.activities.clear()  # ignore the spawn-time publish, if any
    controller._on_agent_hook(_hook(1, "PreToolUse", tool_name="Bash"))
    assert bridge.activities[-1] == {
        "slot": 1,
        "state": "working",
        "tool": "Running",
        "in_plan_mode": False,
        "session_id": "",
    }


def test_local_hook_publishes_cleared_state_on_stop(controller, bridge):
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(_hook(1, "PreToolUse", tool_name="Bash"))
    bridge.activities.clear()
    controller._on_agent_hook(_hook(1, "Stop"))
    # The dashboard must see the slot go quiet — published with empty state.
    assert bridge.activities[-1]["slot"] == 1
    assert bridge.activities[-1]["state"] == ""


def test_local_hook_publishes_session_id(controller, bridge):
    controller.spawn_agent("fresh", True)
    bridge.activities.clear()
    controller._on_agent_hook(_hook(1, "UserPromptSubmit", session_id="sess-abc"))
    assert bridge.activities[-1]["session_id"] == "sess-abc"


def test_local_hook_publishes_plan_mode(controller, bridge):
    controller.spawn_agent("fresh", True)
    bridge.activities.clear()
    controller._on_agent_hook(
        _hook(1, "PreToolUse", tool_name="Bash", permission_mode="plan")
    )
    assert bridge.activities[-1]["in_plan_mode"] is True


def test_session_only_event_publishes_session_id_without_activity(controller, bridge):
    # The session_changed-only path: an observer event that is the FIRST to carry
    # a session_id publishes it even though it makes no activity change.
    controller.spawn_agent("fresh", True)
    bridge.activities.clear()
    controller._on_agent_hook(_hook(1, "FileChanged", session_id="sess-first"))
    assert len(bridge.activities) == 1
    assert bridge.activities[-1]["session_id"] == "sess-first"
    assert bridge.activities[-1]["state"] == ""  # observer → no activity change


def test_observer_event_without_session_change_does_not_publish(controller, bridge):
    controller.spawn_agent("fresh", True)
    bridge.activities.clear()
    controller._on_agent_hook(_hook(1, "FileChanged"))  # observer no-op, no session
    assert bridge.activities == []


# ---------------------------------------------------------------------------
# STT indicator mirroring — MIGRATED (agent-ownership inversion, Phase 4)
#
# The snapshot-"stt"-field → sttTargetSlot/sttTranscribing mirror
# (`_mirror_stt_state`) was removed: STT recording state now arrives on the
# direct IDE socket, not the bridge snapshot. Its coverage lives in the
# "Direct STT channel" section below (test_on_stt_recording_*), which exercises
# the same slot-resolution rules (explicit slot, -1 → focused, 0 → clear).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Central-surface "agent" value
# ---------------------------------------------------------------------------


def test_set_central_surface_accepts_agent(controller):
    controller.set_central_surface("agent")
    assert controller.agentSurfaceVisible is True
    assert controller.editorVisible is False
    assert controller.terminalVisible is False


def test_set_central_surface_rejects_unknown_value(controller):
    controller.set_central_surface("bogus")
    assert controller.centralSurface == "terminal"


def test_toggle_editor_terminal_from_agent_lands_on_editor(controller):
    controller.set_central_surface("agent")
    controller.toggle_editor_terminal()
    assert controller.centralSurface == "editor"


# ---------------------------------------------------------------------------
# Title cleaning (sparkle glyph + default-title suppression)
# ---------------------------------------------------------------------------


def test_title_strips_leading_sparkle_glyph(controller, bridge):
    controller.spawn_agent("fresh", True)
    controller.on_agent_title(1, "✳ fix the flaky tests")
    assert controller.agentTitles[0] == "fix the flaky tests"
    assert bridge.titles == [(1, "fix the flaky tests")]


def test_default_claude_code_title_is_suppressed(controller, bridge):
    controller.spawn_agent("fresh", True)
    controller.on_agent_title(1, "✳ Claude Code")
    assert controller.agentTitles[0] == ""
    # No publish either — "" equals the initial title, so the dedupe
    # guard drops it (the dashboard shows no title until a real one).
    assert bridge.titles == []


def test_bare_claude_code_title_is_suppressed_case_insensitively(controller):
    controller.spawn_agent("fresh", True)
    controller.on_agent_title(1, "claude code")
    assert controller.agentTitles[0] == ""


def test_bare_opencode_title_is_suppressed(controller):
    # OpenCode's placeholder product-name title plays the same role as
    # claude's "Claude Code" — no title until a real session summary.
    controller.spawn_agent("fresh", True, "opencode")
    controller.on_agent_title(1, "OpenCode")
    assert controller.agentTitles[0] == ""


def test_bare_hostname_title_is_suppressed(controller):
    # Under the tmux substrate `set-titles-string "#T"` forwards the pane
    # title, which defaults to the machine hostname before the harness sets
    # its own OSC title. A freshly-spawned chip must not flash the hostname.
    import socket

    controller.spawn_agent("fresh", True)
    controller.on_agent_title(1, socket.gethostname())
    assert controller.agentTitles[0] == ""
    # And case-insensitively, matching the other placeholder guards.
    controller.on_agent_title(1, socket.gethostname().upper())
    assert controller.agentTitles[0] == ""


def test_hostname_substring_is_not_suppressed(controller):
    # The guard is an EXACT (case-insensitive) match, not a substring — a
    # real session summary that merely contains the hostname survives.
    import socket

    real = f"deploy to {socket.gethostname()} box"
    controller.spawn_agent("fresh", True)
    controller.on_agent_title(1, real)
    assert controller.agentTitles[0] == real


# claude's spinner alphabet is unstable across versions — a glyph
# outside the old hardcoded strip set leaked a tofu box into the
# chip AND defeated the "Claude Code" suppression (the screenshot
# bug of 2026-06-11). The regex prefix-strip must handle any
# non-word decoration, including multi-char ones.
@pytest.mark.parametrize(
    "raw",
    ["✽ Claude Code", "· Claude Code", "⠴ Claude Code", "✻ ✽ Claude Code"],
    ids=["heavy-asterisk", "middle-dot", "braille-spinner", "double-glyph"],
)
def test_unlisted_spinner_glyph_still_suppresses_default_title(controller, raw):
    controller.spawn_agent("fresh", True)
    controller.on_agent_title(1, raw)
    assert controller.agentTitles[0] == ""


def test_unlisted_spinner_glyph_strips_from_real_title(controller):
    # The suppression cases above share their strip path with REAL
    # titles — an unlisted glyph on a meaningful session name must be
    # stripped without suppressing the title itself.
    controller.spawn_agent("fresh", True)
    controller.on_agent_title(1, "✽ fix the flaky tests")
    assert controller.agentTitles[0] == "fix the flaky tests"


def test_real_title_after_default_replaces_it(controller, bridge):
    controller.spawn_agent("fresh", True)
    controller.on_agent_title(1, "✳ Claude Code")
    controller.on_agent_title(1, "✶ refactor the bridge client")
    assert controller.agentTitles[0] == "refactor the bridge client"
    assert bridge.titles == [(1, "refactor the bridge client")]


# ---------------------------------------------------------------------------
# Display-order compaction (chip numbers are dense positions, not slots)
# ---------------------------------------------------------------------------


def test_close_first_agent_promotes_survivor_to_position_one(controller):
    controller.spawn_agent("fresh", True)  # internal slot 1, position 1
    controller.spawn_agent("fresh", True)  # internal slot 2, position 2
    controller.close_agent(1)
    # Survivor (internal slot 2) is now display position 1...
    assert controller.agentOrder == [2]
    # ...while its frozen identity (SYMMETRIA_AGENT_ID env of the live
    # process) keeps the internal slot number.
    agent_id_pair = next(
        a for a in controller.agent_spawn_argv(2) if a.startswith("SYMMETRIA_AGENT_ID=")
    )
    assert agent_id_pair.endswith("_2")


def test_spawn_after_compaction_appends_as_next_position(controller):
    controller.spawn_agent("fresh", True)  # slot 1
    controller.spawn_agent("fresh", True)  # slot 2
    controller.close_agent(1)
    controller.spawn_agent("fresh", True)  # reuses internal slot 1
    # Display order: survivor first (position 1), newcomer appended
    # (position 2) — internal slot numbers are NOT the display order.
    assert controller.agentOrder == [2, 1]


def test_cycle_follows_display_order_after_compaction(controller):
    controller.spawn_agent("fresh", True)  # slot 1
    controller.spawn_agent("fresh", True)  # slot 2
    controller.close_agent(1)
    controller.spawn_agent("fresh", True)  # slot 1 again, position 2
    controller.focus_agent(2)  # position 1
    controller.cycle_agent_focus(1)
    assert controller.focusedAgent == 1  # position 2 (internal slot 1)
    controller.cycle_agent_focus(1)
    assert controller.focusedAgent == 2  # wrapped to position 1


# ---------------------------------------------------------------------------
# STT inject validation (_dispatch_inject — the shared validate-and-deliver
# step). Since the agent-ownership inversion (Phase 4) it is reached only via
# the direct channel (_on_stt_inject); the `fail` callback is injected so the
# validation is tested decoupled from the reply channel.
#
# Scope: Python-side validation/routing only. The QML delivery half
# (bracketed paste, settle timer, busy/no-pane/agent-closed replies in
# Main.qml's agentSurface) is out of unit-test reach — verified manually
# and guarded by comments at the QML site.
# ---------------------------------------------------------------------------


def _capture_inject_emissions(controller):
    emitted: list[tuple[int, str, bool, str]] = []
    controller.agentInjectRequested.connect(
        lambda slot, text, submit, rid: emitted.append((slot, text, submit, rid))
    )
    return emitted


def test_inject_routes_to_requested_slot(controller):
    controller.spawn_agent("fresh", True)  # slot 1
    controller.spawn_agent("fresh", True)  # slot 2
    emitted = _capture_inject_emissions(controller)
    fails: list[tuple[str, str]] = []
    controller._dispatch_inject(
        {"request_id": "r1", "buf": 2, "text": "hola", "submit": True},
        lambda rid, err: fails.append((rid, err)),
    )
    assert emitted == [(2, "hola", True, "r1")]
    assert fails == []  # QML closes the loop, the fail callback is untouched


def test_inject_dead_slot_falls_back_to_focused(controller):
    controller.spawn_agent("fresh", True)  # slot 1, focused
    emitted = _capture_inject_emissions(controller)
    controller._dispatch_inject(
        {"request_id": "r2", "buf": 4, "text": "hola", "submit": False},
        lambda rid, err: None,
    )
    assert emitted == [(1, "hola", False, "r2")]


def test_inject_with_no_agents_fails_fast(controller):
    emitted = _capture_inject_emissions(controller)
    fails: list[tuple[str, str]] = []
    controller._dispatch_inject(
        {"request_id": "r3", "buf": 1, "text": "hola", "submit": True},
        lambda rid, err: fails.append((rid, err)),
    )
    assert emitted == []
    assert fails == [("r3", "no-agent")]


def test_inject_empty_text_fails_fast(controller):
    controller.spawn_agent("fresh", True)
    fails: list[tuple[str, str]] = []
    controller._dispatch_inject(
        {"request_id": "r4", "buf": 1, "text": ""},
        lambda rid, err: fails.append((rid, err)),
    )
    assert fails == [("r4", "empty-text")]


def test_inject_strips_escape_characters(controller):
    # An embedded ESC could terminate the QML-side bracketed paste early
    # (\x1b[201~) and leak the remainder as live keystrokes.
    controller.spawn_agent("fresh", True)
    emitted = _capture_inject_emissions(controller)
    controller._dispatch_inject(
        {"request_id": "r5", "buf": 1, "text": "a\x1b[201~rm -rf", "submit": False},
        lambda rid, err: None,
    )
    assert emitted == [(1, "a[201~rm -rf", False, "r5")]


def test_inject_missing_request_id_is_ignored(controller):
    controller.spawn_agent("fresh", True)
    emitted = _capture_inject_emissions(controller)
    fails: list[tuple[str, str]] = []
    controller._dispatch_inject(
        {"buf": 1, "text": "hola"},
        lambda rid, err: fails.append((rid, err)),
    )
    assert emitted == []
    assert fails == []  # no id to reply to → dropped without calling fail


def test_agent_inject_done_resolves_pending_direct_inject(controller):
    # agent_inject_done resolves the agent-events Future for this request_id,
    # unblocking the connection handler waiting to reply to the dictation client.
    fut: concurrent.futures.Future = concurrent.futures.Future()
    controller._agent_events._pending_injects["r6"] = fut
    controller.agent_inject_done("r6", True, True, "")
    assert fut.result(timeout=1) == {"ok": True, "submitted": True, "error": ""}


def test_agent_inject_done_unknown_request_id_is_a_no_op(controller):
    # An id with no pending inject (already resolved / timed out) is benign.
    controller.agent_inject_done("nope", True, True, "")  # must not raise


# ---------------------------------------------------------------------------
# Direct STT channel (inversion P4): _on_stt_inject + _on_stt_recording
# ---------------------------------------------------------------------------


def test_on_stt_inject_routes_to_slot(controller):
    # The agent-events server stamps request_id before emitting, so the payload
    # carries it. Delivery is the same QML path as the bridge inject.
    controller.spawn_agent("fresh", True)
    emitted = _capture_inject_emissions(controller)
    controller._on_stt_inject(
        {"request_id": "d1", "buf": 1, "text": "hi", "submit": True}
    )
    assert emitted == [(1, "hi", True, "d1")]


def test_on_stt_inject_no_agent_is_a_pre_delivery_failure(controller):
    # A pre-delivery failure on the direct path resolves the agent-events Future
    # (here it's pending, so we can observe the structured failure reply) and
    # never emits a delivery request.
    fut: concurrent.futures.Future = concurrent.futures.Future()
    controller._agent_events._pending_injects["d2"] = fut
    emitted = _capture_inject_emissions(controller)
    controller._on_stt_inject({"request_id": "d2", "buf": 1, "text": "hi"})
    assert emitted == []
    assert fut.result(timeout=1) == {
        "ok": False,
        "submitted": False,
        "error": "no-agent",
    }


def test_on_stt_recording_drives_dot(controller):
    controller.spawn_agent("fresh", True)
    controller._on_stt_recording({"buf": 1, "transcribing": True})
    assert controller.sttTargetSlot == 1
    assert controller.sttTranscribing is True


def test_on_stt_recording_buf_zero_clears(controller):
    controller.spawn_agent("fresh", True)
    controller._on_stt_recording({"buf": 1, "transcribing": True})
    controller._on_stt_recording({"buf": 0})
    assert controller.sttTargetSlot == 0
    assert controller.sttTranscribing is False


def test_on_stt_recording_minus_one_resolves_focused(controller):
    controller.spawn_agent("fresh", True)
    controller.spawn_agent("fresh", True)
    controller.focus_agent(2)
    controller._on_stt_recording({"buf": -1, "transcribing": True})
    assert controller.sttTargetSlot == 2


def test_on_stt_recording_dead_slot_is_ignored(controller):
    controller.spawn_agent("fresh", True)
    controller._on_stt_recording({"buf": 4, "transcribing": True})
    assert controller.sttTargetSlot == 0
    assert controller.sttTranscribing is False


def test_on_stt_recording_emits_only_on_change(controller):
    controller.spawn_agent("fresh", True)
    emissions: list[None] = []
    controller.sttStateChanged.connect(lambda: emissions.append(None))
    controller._on_stt_recording({"buf": 1, "transcribing": True})
    controller._on_stt_recording({"buf": 1, "transcribing": True})
    assert len(emissions) == 1


def test_on_stt_recording_non_int_buf_is_tolerated(controller):
    # A malformed buf must not raise or latch the indicator.
    controller.spawn_agent("fresh", True)
    controller._on_stt_recording({"buf": "garbage", "transcribing": True})
    assert controller.sttTargetSlot == 0
    assert controller.sttTranscribing is False


def test_on_stt_recording_minus_one_empty_pool_clears(controller):
    # buf -1 with no agents: the focused-slot fallback resolves to 0, so the
    # indicator must stay dark and transcribing must not latch.
    controller._on_stt_recording({"buf": -1, "transcribing": True})
    assert controller.sttTargetSlot == 0
    assert controller.sttTranscribing is False


# ---------------------------------------------------------------------------
# Failed-start detection: an agent whose process dies within
# _AGENT_FAST_DEATH_SECONDS of spawn surfaces a toast (agentSpawnFailed)
# instead of silently flapping the chip. See on_agent_finished.
# ---------------------------------------------------------------------------


def _capture_spawn_failed(controller) -> list[tuple[str, str]]:
    captured: list[tuple[str, str]] = []
    controller.agentSpawnFailed.connect(
        lambda title, detail: captured.append((title, detail))
    )
    return captured


def test_fast_death_emits_spawn_failed_and_cleans_up(controller, bridge):
    # A just-spawned agent whose process exits immediately (lifetime ≈ 0, well
    # under the threshold) is a FAILED START: alert AND clean removal.
    captured = _capture_spawn_failed(controller)
    controller.spawn_agent("fresh", True)
    assert controller.agentOrder == [1]

    controller.on_agent_finished(1)

    assert len(captured) == 1
    title, detail = captured[0]
    assert "#1" in title
    assert "claude" in title
    assert detail  # a non-empty explanation (memory note or generic)
    # Still cleaned up exactly like a normal close.
    assert controller.agentOrder == []
    assert bridge.removes == [1]


def test_slow_exit_does_not_emit_spawn_failed(controller, bridge):
    # An agent that lived past the threshold before exiting (normal /exit or a
    # long session) must NOT raise the failed-start alert — only get removed.
    captured = _capture_spawn_failed(controller)
    controller.spawn_agent("fresh", True)
    controller._term_agents[1]["spawn_mono"] = (
        time.monotonic() - controller._AGENT_FAST_DEATH_SECONDS - 10
    )

    controller.on_agent_finished(1)

    assert captured == []
    assert controller.agentOrder == []
    assert bridge.removes == [1]


def test_user_close_then_finished_does_not_false_alert(controller, bridge):
    # Ctrl+Shift+Q path: close_agent removes the slot, then the Loader teardown
    # fires onFinished. on_agent_finished must no-op (slot already gone) — no
    # false failed-start alert and no double remove.
    captured = _capture_spawn_failed(controller)
    controller.spawn_agent("fresh", True)

    controller.close_agent(1)  # explicit user close
    controller.on_agent_finished(1)  # late onFinished from the teardown

    assert captured == []
    assert bridge.removes == [1]  # exactly one remove


def test_fast_death_reports_display_position_not_internal_slot(controller):
    # The alert numbers agents by dense DISPLAY position (the chip number), not
    # the frozen internal slot. Force the two to diverge: free a middle slot so
    # the next spawn reuses that internal slot but lands LAST in display order.
    captured = _capture_spawn_failed(controller)
    controller.spawn_agent("fresh", True)  # internal slot 1
    controller.spawn_agent("fresh", True)  # internal slot 2
    controller.spawn_agent("fresh", True)  # internal slot 3
    controller.close_agent(2)  # frees internal slot 2
    controller.spawn_agent("fresh", True)  # reuses slot 2, appended last
    assert controller.agentOrder == [1, 3, 2]  # internal slot 2 → display #3

    controller.on_agent_finished(2)

    assert len(captured) == 1
    # Internal slot is 2, but its display position is 3 → the title must read #3.
    assert "#3" in captured[0][0]
    assert "#2" not in captured[0][0]


def test_on_agent_finished_unknown_slot_is_a_no_op(controller, bridge):
    captured = _capture_spawn_failed(controller)
    controller.on_agent_finished(3)  # never spawned
    assert captured == []
    assert bridge.removes == []


def test_memory_pressure_note_returns_bool_and_string(controller):
    # Smoke test the real /proc/meminfo parse: returns a (bool, str) pair and
    # never raises on this host.
    from symmetria_ide.app import _memory_pressure_note

    low, note = _memory_pressure_note()
    assert isinstance(low, bool)
    assert isinstance(note, str)


def _patch_meminfo(monkeypatch, content: str) -> None:
    """Feed synthetic /proc/meminfo to _memory_pressure_note via builtins.open.

    The helper calls open() exactly once and monkeypatch reverts at teardown,
    so the global patch window contains only that read.
    """
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(content))


def test_memory_pressure_note_low_ram_and_swap_is_low(monkeypatch):
    from symmetria_ide.app import _memory_pressure_note

    _patch_meminfo(
        monkeypatch,
        "MemAvailable:   200000 kB\nSwapTotal: 16000000 kB\nSwapFree:    50000 kB\n",
    )
    low, note = _memory_pressure_note()
    assert low is True
    assert "RAM" in note and "swap" in note


def test_memory_pressure_note_no_swap_avoids_100pct_wording(monkeypatch):
    # Regression for the no-swap message bug: a box with no swap configured,
    # under RAM pressure, must NOT read "swap 100% free" (which would contradict
    # the low-memory warning) — it says "no swap configured" instead.
    from symmetria_ide.app import _memory_pressure_note

    _patch_meminfo(
        monkeypatch,
        "MemAvailable:   100000 kB\nSwapTotal:        0 kB\nSwapFree:         0 kB\n",
    )
    low, note = _memory_pressure_note()
    assert low is True
    assert "100% free" not in note
    assert "no swap configured" in note


def test_memory_pressure_note_ample_ram_is_not_low(monkeypatch):
    # Plenty of reclaimable RAM → not low even with zero swap.
    from symmetria_ide.app import _memory_pressure_note

    _patch_meminfo(
        monkeypatch,
        "MemAvailable: 8000000 kB\nSwapTotal:        0 kB\nSwapFree:         0 kB\n",
    )
    low, _note = _memory_pressure_note()
    assert low is False


def test_memory_pressure_note_unreadable_file_degrades(monkeypatch):
    from symmetria_ide.app import _memory_pressure_note

    def _boom(*_a, **_k):
        raise OSError("nope")

    monkeypatch.setattr("builtins.open", _boom)
    assert _memory_pressure_note() == (False, "")


def test_memory_pressure_note_skips_malformed_lines(monkeypatch):
    # A colon-less line and a value-less line must be skipped, not abort the
    # whole parse — the real fields still come through.
    from symmetria_ide.app import _memory_pressure_note

    _patch_meminfo(
        monkeypatch,
        "garbage line no colon\nMemAvailable:   200000 kB\nWeird:\n"
        "SwapTotal: 16000000 kB\nSwapFree:    50000 kB\n",
    )
    low, note = _memory_pressure_note()
    assert low is True  # parsed despite the bad lines
    assert "195 MB" in note  # 200000 kB / 1024, rounded


# ---------------------------------------------------------------------------
# Status-line tap → _on_status_line (per-agent fields + account usage)
# ---------------------------------------------------------------------------


class FakeUsageStore:
    """Captures publishes; hermetic (no real shared file written/watched)."""

    def __init__(self) -> None:
        self.published: list[dict] = []
        self._current: dict | None = None

    def publish(self, usage: dict) -> None:
        self.published.append(dict(usage))
        self._current = dict(usage)

    def read_current(self) -> dict | None:
        return self._current

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


@pytest.fixture
def usage_controller(controller):
    # Swap the real (default-path) store for a hermetic fake so nothing touches
    # /run/user/$UID/symmetria-ide-account-usage.json during tests.
    controller._account_usage_store = FakeUsageStore()
    return controller


def _status_line(slot: int, **fields) -> dict:
    return {"type": "status_line", "agent_id": f"{os.getpid()}_{slot}", **fields}


def test_status_line_stores_per_agent_fields(usage_controller):
    c = usage_controller
    c.spawn_agent("fresh", True)
    c._on_status_line(_status_line(1, model="Opus 4.8", effort="high", context_pct=42))
    assert c.agentModels[0] == "Opus 4.8"
    assert c.agentEfforts[0] == "high"
    assert c.agentContextPct[0] == 42


def test_status_line_context_default_is_minus_one(usage_controller):
    c = usage_controller
    c.spawn_agent("fresh", True)
    # No context_pct reported → property default -1 (so QML hides "unknown").
    assert c.agentContextPct[0] == -1


def test_status_line_stores_context_display_tokens(usage_controller):
    c = usage_controller
    c.spawn_agent("fresh", True)
    c._on_status_line(_status_line(1, context_pct=14, context_display="137k/1000k"))
    assert c.agentContextPct[0] == 14
    assert c.agentContextDisplay[0] == "137k/1000k"


def test_status_line_context_display_default_empty(usage_controller):
    c = usage_controller
    c.spawn_agent("fresh", True)
    assert c.agentContextDisplay[0] == ""


def test_status_line_ignores_foreign_pid(usage_controller):
    c = usage_controller
    c.spawn_agent("fresh", True)
    c._on_status_line({"type": "status_line", "agent_id": "99999_1", "model": "X"})
    assert c.agentModels[0] == ""


def test_status_line_ignores_unknown_slot(usage_controller):
    c = usage_controller
    c.spawn_agent("fresh", True)
    c._on_status_line(_status_line(4, model="X"))
    assert all(m == "" for m in c.agentModels)


def test_status_line_emits_status_changed_only_on_change(usage_controller):
    c = usage_controller
    c.spawn_agent("fresh", True)
    emissions: list[None] = []
    c.agentStatusChanged.connect(lambda: emissions.append(None))
    payload = _status_line(
        1, model="Opus 4.8", effort="high", context_pct=42, context_display="34k/200k"
    )
    c._on_status_line(payload)
    c._on_status_line(dict(payload))  # identical re-send → no emit
    assert len(emissions) == 1


def test_account_usage_freshest_wins(usage_controller):
    c = usage_controller
    c.spawn_agent("fresh", True)
    rl = {
        "five_hour": {"pct": 20, "resets_at": 100},
        "seven_day": {"pct": 50, "resets_at": 200},
    }
    c._on_status_line(_status_line(1, rate_limits=rl, observed_at_ns=1000))
    assert c.accountUsageValid is True
    assert c.accountUsage5hPct == 20
    assert c.accountUsage7dPct == 50
    assert c.accountUsage5hReset == 100
    # An OLDER observation must NOT overwrite the freshest.
    older = {
        "five_hour": {"pct": 99, "resets_at": 1},
        "seven_day": {"pct": 99, "resets_at": 1},
    }
    c._on_status_line(_status_line(1, rate_limits=older, observed_at_ns=500))
    assert c.accountUsage5hPct == 20  # unchanged
    # A NEWER one wins.
    newer = {
        "five_hour": {"pct": 33, "resets_at": 9},
        "seven_day": {"pct": 44, "resets_at": 9},
    }
    c._on_status_line(_status_line(1, rate_limits=newer, observed_at_ns=2000))
    assert c.accountUsage5hPct == 33
    assert c.accountUsage7dPct == 44


def test_account_usage_coerces_float_percentage(usage_controller):
    c = usage_controller
    c.spawn_agent("fresh", True)
    rl = {"five_hour": {"pct": 23.5, "resets_at": 100}}
    c._on_status_line(_status_line(1, rate_limits=rl, observed_at_ns=1000))
    assert c.accountUsage5hPct == 23  # truncated, like the bash status line


def test_account_usage_publishes_to_peer_when_fresher(usage_controller):
    c = usage_controller
    c.spawn_agent("fresh", True)
    rl = {"five_hour": {"pct": 20, "resets_at": 100}}
    c._on_status_line(_status_line(1, rate_limits=rl, observed_at_ns=1000))
    assert len(c._account_usage_store.published) == 1
    assert c._account_usage_store.published[0]["observed_at_ns"] == 1000
    # An older observation neither adopts nor re-publishes.
    c._on_status_line(_status_line(1, rate_limits=rl, observed_at_ns=500))
    assert len(c._account_usage_store.published) == 1


def test_stop_hook_records_done_at(controller):
    # The agent finishing a turn (Stop hook) stamps agentDoneAt for the status
    # bar's "done HH:MM" — mirroring the bash stop-timestamp.sh signal.
    controller.spawn_agent("fresh", True)
    assert controller.agentDoneAt[0] == 0
    # The agentStatusChanged emit is the load-bearing path that makes QML
    # re-render "Done at" — assert it fires, not just that the value is stored.
    emissions: list[None] = []
    controller.agentStatusChanged.connect(lambda: emissions.append(None))
    controller._on_agent_hook(
        {
            "agent_id": f"{os.getpid()}_1",
            "hook_event_name": "Stop",
            "session_id": "s1",
        }
    )
    assert controller.agentDoneAt[0] > 0
    assert len(emissions) == 1


def test_non_stop_hook_leaves_done_at_unset(controller):
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(
        {
            "agent_id": f"{os.getpid()}_1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
        }
    )
    assert controller.agentDoneAt[0] == 0


def test_account_usage_invalid_before_any_observation(controller):
    # Uses the real store (never started/written) — purely reads the in-memory
    # default, so no file I/O.
    assert controller.accountUsageValid is False


def test_on_shared_usage_adopts_fresher_peer_value(usage_controller):
    c = usage_controller
    # A peer IDE wrote a value fresher than anything local.
    c._account_usage_store._current = {
        "five_pct": 77,
        "five_reset": 5,
        "seven_pct": 88,
        "seven_reset": 6,
        "observed_at_ns": 9999,
    }
    c._on_shared_usage_changed()
    assert c.accountUsage5hPct == 77
    assert c.accountUsage7dPct == 88
    assert c.accountUsageValid is True


def test_on_shared_usage_ignores_staler_peer_value(usage_controller):
    c = usage_controller
    c.spawn_agent("fresh", True)
    rl = {"five_hour": {"pct": 20, "resets_at": 100}}
    c._on_status_line(_status_line(1, rate_limits=rl, observed_at_ns=5000))
    # Peer file is staler than what we've already observed locally → no adopt.
    c._account_usage_store._current = {
        "five_pct": 99,
        "five_reset": 1,
        "seven_pct": 99,
        "seven_reset": 1,
        "observed_at_ns": 1000,
    }
    c._on_shared_usage_changed()
    assert c.accountUsage5hPct == 20  # local fresher value retained


# ---------------------------------------------------------------------------
# Worktree follow — live cwd capture + focused-agent chrome re-rooting
# ---------------------------------------------------------------------------


@pytest.fixture
def worktree_env(tmp_path):
    """(main_root, worktree_root): a fake main checkout + linked worktree."""
    main = tmp_path / "proj"
    (main / ".git").mkdir(parents=True)
    wt = tmp_path / "proj-wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/feature-x\n")
    return str(main), str(wt)


def _push_cwd(ctrl, path: str) -> None:
    """Simulate one `cwd` capsule (same shape test_anchor_state uses)."""
    ctrl._route_capsule({"id": "cwd", "label": "", "value": path})


def test_hook_cwd_stored_as_live_cwd_not_cwd(controller, worktree_env):
    main, wt = worktree_env
    _push_cwd(controller, main)
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(
        _hook(1, "PreToolUse", tool_name="Bash", session_id="s1", cwd=wt)
    )
    rec = controller._term_agents[1]
    # The spawn-time cwd is load-bearing for attribution/bridge/tmux naming —
    # the live cwd lands in its own field.
    assert rec["cwd"] == main
    assert rec["live_cwd"] == wt


def test_focused_worktree_agent_reroots_chrome(controller, worktree_env):
    main, wt = worktree_env
    _push_cwd(controller, main)
    controller.spawn_agent("fresh", True)  # slot 1, focused
    controller._on_agent_hook(
        _hook(1, "PreToolUse", tool_name="Bash", session_id="s1", cwd=wt)
    )
    assert controller.displayedRoot == wt
    assert controller.displayingWorktree == "feature-x"
    assert controller.agentWorktree[0] == "feature-x"


def test_unfocused_agent_hook_does_not_reroot(controller, worktree_env):
    main, wt = worktree_env
    _push_cwd(controller, main)
    controller.spawn_agent("fresh", True)  # slot 1
    controller.spawn_agent("fresh", True)  # slot 2 — focused (spawn focuses)
    controller._on_agent_hook(
        _hook(1, "PreToolUse", tool_name="Bash", session_id="s1", cwd=wt)
    )
    # Slot 1's glyph lights, but the chrome follows only the FOCUSED agent.
    assert controller.agentWorktree[0] == "feature-x"
    assert controller.displayedRoot == main
    assert controller.displayingWorktree == ""


def test_focus_switch_follows_and_returns(controller, worktree_env):
    main, wt = worktree_env
    _push_cwd(controller, main)
    controller.spawn_agent("fresh", True)  # slot 1
    controller.spawn_agent("fresh", True)  # slot 2 — focused
    controller._on_agent_hook(
        _hook(1, "PreToolUse", tool_name="Bash", session_id="s1", cwd=wt)
    )
    controller.focus_agent(1)
    assert controller.displayedRoot == wt
    assert controller.displayingWorktree == "feature-x"
    # Back to a main-checkout agent → chrome snaps back.
    controller.focus_agent(2)
    assert controller.displayedRoot == main
    assert controller.displayingWorktree == ""


def test_focused_agent_cd_out_clears_immediately(controller, worktree_env):
    main, wt = worktree_env
    _push_cwd(controller, main)
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(
        _hook(1, "PreToolUse", tool_name="Bash", session_id="s1", cwd=wt)
    )
    assert controller.displayedRoot == wt
    controller._on_agent_hook(
        _hook(1, "PostToolUse", tool_name="Bash", session_id="s1", cwd=main)
    )
    assert controller.displayedRoot == main
    assert controller.displayingWorktree == ""
    assert controller.agentWorktree[0] == ""


def test_foreign_repo_root_does_not_follow(controller, worktree_env, tmp_path):
    main, _wt = worktree_env
    foreign = tmp_path / "other"
    (foreign / ".git").mkdir(parents=True)
    _push_cwd(controller, main)
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(
        _hook(1, "PreToolUse", tool_name="Bash", session_id="s1", cwd=main)
    )
    controller._on_agent_hook(
        _hook(1, "PostToolUse", tool_name="Bash", session_id="s1", cwd=str(foreign))
    )
    # v1 scope: only same-repo linked worktrees follow — a foreign repo clears.
    assert controller.displayedRoot == main
    assert controller.displayingWorktree == ""
    assert controller.agentWorktree[0] == ""


def test_shell_cd_sticky_while_following(controller, worktree_env):
    main, wt = worktree_env
    _push_cwd(controller, main)
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(
        _hook(1, "PreToolUse", tool_name="Bash", session_id="s1", cwd=wt)
    )
    _push_cwd(controller, "/tmp/wandering")
    # Sticky-until-re-caused: the shell cd updates _cwd silently only.
    assert controller.cwd == "/tmp/wandering"
    assert controller.displayedRoot == wt


def test_close_last_agent_clears_follow(controller, worktree_env):
    main, wt = worktree_env
    _push_cwd(controller, main)
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(
        _hook(1, "PreToolUse", tool_name="Bash", session_id="s1", cwd=wt)
    )
    assert controller.displayedRoot == wt
    controller.close_agent(1)
    assert controller.displayedRoot == main
    assert controller.displayingWorktree == ""


def test_spawn_while_following_spawns_in_main(controller, worktree_env):
    """New agents ALWAYS spawn in the main checkout (user decision 2026-07-13,
    reversing the short-lived "everything follows" spawn semantics): even with
    the chrome re-rooted to a worktree, a fresh agent starts from the
    project's source of truth."""
    main, wt = worktree_env
    _push_cwd(controller, main)
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(
        _hook(1, "PreToolUse", tool_name="Bash", session_id="s1", cwd=wt)
    )
    assert controller.displayedRoot == wt  # follow active
    controller.spawn_agent("fresh", True)  # slot 2
    rec = controller._term_agents[2]
    assert rec["cwd"] == main
    assert controller.agentWorktree[1] == ""
    # Spawning routes through focus_agent(2) — a main-checkout agent — so the
    # follow CLEARS and the chrome returns to main with the new agent.
    assert controller.displayedRoot == main


def test_spawn_from_manually_displayed_worktree_spawns_in_main(
    controller, worktree_env
):
    """Same rule when the user cd'd/anchored the chrome into a worktree with
    no follow involved: the spawn redirects to the canonical main root."""
    main, wt = worktree_env
    _push_cwd(controller, wt)
    controller.spawn_agent("fresh", True)
    assert controller._term_agents[1]["cwd"] == main
    assert controller.agentWorktree[0] == ""


def test_agent_worktree_signal_emits_only_on_change(controller, worktree_env):
    main, wt = worktree_env
    _push_cwd(controller, main)
    controller.spawn_agent("fresh", True)
    emissions: list[None] = []
    controller.agentWorktreeChanged.connect(lambda: emissions.append(None))
    hook = _hook(1, "PreToolUse", tool_name="Bash", session_id="s1", cwd=wt)
    controller._on_agent_hook(hook)
    controller._on_agent_hook(dict(hook, hook_event_name="PostToolUse"))
    assert len(emissions) == 1


# ---------------------------------------------------------------------------
# Worktree follow via WRITE-tool paths (tool_path) — the session cwd never
# moves when an agent works in a worktree through Bash, so the reporter
# forwards each tool's target path and the IDE follows where edits land.
# ---------------------------------------------------------------------------


def test_write_tool_path_in_worktree_follows(controller, worktree_env):
    main, wt = worktree_env
    _push_cwd(controller, main)
    controller.spawn_agent("fresh", True)
    # Session cwd stays in main (the magistralia case) — only the Edit's
    # file_path reveals the worktree.
    controller._on_agent_hook(
        _hook(
            1,
            "PreToolUse",
            tool_name="Edit",
            tool_path=f"{wt}/src/app.py",
            session_id="s1",
            cwd=main,
        )
    )
    assert controller._term_agents[1]["work_root"] == wt
    assert controller.displayedRoot == wt
    assert controller.displayingWorktree == "feature-x"
    assert controller.agentWorktree[0] == "feature-x"


def test_read_tool_path_does_not_follow(controller, worktree_env):
    main, wt = worktree_env
    _push_cwd(controller, main)
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(
        _hook(
            1,
            "PreToolUse",
            tool_name="Read",
            tool_path=f"{wt}/README.md",
            session_id="s1",
            cwd=main,
        )
    )
    # Exploration must not yank the chrome — only write tools count.
    assert "work_root" not in controller._term_agents[1]
    assert controller.displayedRoot == main
    assert controller.displayingWorktree == ""


def test_write_back_in_main_returns(controller, worktree_env):
    main, wt = worktree_env
    _push_cwd(controller, main)
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(
        _hook(
            1,
            "PreToolUse",
            tool_name="Edit",
            tool_path=f"{wt}/src/app.py",
            session_id="s1",
            cwd=main,
        )
    )
    assert controller.displayedRoot == wt
    controller._on_agent_hook(
        _hook(
            1,
            "PreToolUse",
            tool_name="Write",
            tool_path=f"{main}/notes.md",
            session_id="s1",
            cwd=main,
        )
    )
    # Last write wins: editing the main checkout pulls the chrome back.
    assert controller.displayedRoot == main
    assert controller.displayingWorktree == ""
    assert controller.agentWorktree[0] == ""


def test_tool_path_never_touches_spawn_cwd(controller, worktree_env):
    main, wt = worktree_env
    _push_cwd(controller, main)
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(
        _hook(
            1,
            "PreToolUse",
            tool_name="Edit",
            tool_path=f"{wt}/src/app.py",
            session_id="s1",
            cwd=main,
        )
    )
    rec = controller._term_agents[1]
    # Attribution/bridge/tmux still key on the spawn-time cwd.
    assert rec["cwd"] == main
    assert rec["live_cwd"] == main


# ---------------------------------------------------------------------------
# Vanished roots — a project root deleted under a live IDE (2026-07-18).
#
# Live failure: an agent worked inside a Claude-Code-native worktree
# (`<repo>/.claude/worktrees/<name>`), the follow re-rooted the chrome there,
# and the worktree was then removed. The chrome kept naming the dead path, and
# the next agent spawned INTO it — QProcess fails such a start as
# FailedToStart, which emits no `finished`, so the slot was never dropped and
# the pane stayed black forever under a live-looking chip.
#
# Note the spawn-to-main redirect could not save it: `resolve_project_root`
# walks UP to the first ancestor holding `.git`, so on a deleted worktree it
# silently returns the MAIN root, `linked_worktree_info` then answers "not a
# worktree", and no redirect fires. The guard is only reachable while the
# worktree still exists — hence the repair below.
# ---------------------------------------------------------------------------


def test_existing_root_passes_through_a_live_directory(tmp_path):
    live = str(tmp_path)
    assert _existing_root(live, str(tmp_path)) == live


def test_existing_root_falls_back_to_nearest_live_ancestor(tmp_path):
    ghost = tmp_path / "repo" / ".claude" / "worktrees" / "gone"
    (tmp_path / "repo").mkdir()
    assert _existing_root(str(ghost), str(tmp_path)) == str(tmp_path / "repo")


def test_existing_root_floors_at_home_instead_of_filesystem_root(tmp_path):
    # Walking up from a fully dead absolute path must not land on "/": starting
    # a shell — or rooting a recursive watcher — there is a worse failure than
    # the one being fixed (see _clamp_editor_root's thermal history).
    home = str(tmp_path)
    assert _existing_root("/nonexistent-top/nested/deeper", home) == home


def test_existing_root_leaves_empty_untouched(tmp_path):
    assert _existing_root("", str(tmp_path)) == ""


def test_deleted_followed_worktree_releases_the_chrome(controller, worktree_env):
    # ANCHORING into the worktree is what makes this test pin the repair. The
    # override alone is released without it: deleting a worktree takes its .git
    # pointer file with it, so linked_worktree_info() already answers ("","")
    # and the pre-existing recompute clears the follow on its own. Only the
    # anchor survives a deletion and needs _drop_vanished_roots to release it —
    # verified by stubbing the repair to a no-op, which leaves this test failing
    # and the override-only version passing.
    main, wt = worktree_env
    _push_cwd(controller, main)
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(
        _hook(1, "PreToolUse", tool_name="Write", tool_path=f"{wt}/a.py", cwd=main)
    )
    assert controller.displayedRoot == wt  # following
    controller.anchor_to_path(wt)

    shutil.rmtree(wt)
    controller._recompute_agent_root_override()

    assert controller.anchored is False
    assert controller.displayedRoot == main
    assert controller.displayingWorktree == ""
    # The chip's branch glyph must clear too — it is refreshed from hooks, and
    # a deleted worktree produces no further hook to trigger one.
    assert controller.agentWorktree[0] == ""


def test_drop_vanished_roots_is_inert_in_vps_location(controller, tmp_path):
    # In vps the roots are server paths behind an SSHFS mount: stat'ing them
    # blocks the GUI thread on a hung mount and answers False on a stale one,
    # which would release a live anchor because the network hiccuped.
    live = tmp_path / "live"
    live.mkdir()
    _push_cwd(controller, str(live))
    controller.anchor_to_path("/srv/definitely/not/here")
    controller._location = "vps"

    controller._drop_vanished_roots()

    assert controller.anchored is True
    assert controller.displayedRoot == "/srv/definitely/not/here"


def test_unreachable_mount_does_not_release_the_anchor(
    controller, tmp_path, monkeypatch
):
    # ENOTCONN (stale SSHFS) means "still there, unreachable" — only ENOENT
    # means gone. Releasing on the former yanks the chrome out from under a
    # user whose mount merely flapped.
    live = tmp_path / "live"
    live.mkdir()
    mount = tmp_path / "mount"
    mount.mkdir()
    _push_cwd(controller, str(live))
    controller.anchor_to_path(str(mount))

    def _stale(path):
        raise OSError(errno.ENOTCONN, "Transport endpoint is not connected")

    monkeypatch.setattr("symmetria_ide.app.os.lstat", _stale)
    controller._drop_vanished_roots()

    assert controller.anchored is True
    assert controller.displayedRoot == str(mount)


def test_agent_tmux_socket_defaults_to_the_shared_vigilia_socket(monkeypatch):
    # The conftest fixture sets this env for every test, so the default branch
    # is otherwise unreachable from the suite — and that default is precisely
    # the value whose reach caused the self-kill incident.
    monkeypatch.delenv("SYMMETRIA_IDE_TMUX_SOCKET", raising=False)
    assert _agent_tmux_socket() == os.path.expanduser("~/.vigilia/tmux.sock")


def test_deleted_anchor_is_released(controller, tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    doomed = tmp_path / "doomed"
    doomed.mkdir()
    _push_cwd(controller, str(live))
    controller.anchor_to_path(str(doomed))
    assert controller.displayedRoot == str(doomed)

    shutil.rmtree(doomed)
    controller._drop_vanished_roots()

    # An anchor to a directory that no longer exists is not usable state: every
    # read of it is wrong and every spawn from it fails.
    assert controller.anchored is False
    assert controller.displayedRoot == str(live)


def test_spawn_after_worktree_deletion_uses_a_real_directory(controller, worktree_env):
    main, wt = worktree_env
    _push_cwd(controller, main)
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(
        _hook(1, "PreToolUse", tool_name="Write", tool_path=f"{wt}/a.py", cwd=main)
    )
    shutil.rmtree(wt)

    controller.spawn_agent("fresh", True)  # slot 2 — the spawn that used to die

    assert controller._term_agents[2]["cwd"] == main
    assert os.path.isdir(controller.agent_start_dir(2))


def test_agent_start_dir_uses_the_record_not_the_live_chrome(controller, worktree_env):
    # The pty's working directory must come from the slot record, so the
    # "new agents always spawn in the MAIN checkout" redirect holds even off
    # the tmux path (where the outer chdir IS the agent's cwd). Binding it to
    # displayedRoot instead made that decision a silent no-op.
    main, wt = worktree_env
    _push_cwd(controller, main)
    controller.spawn_agent("fresh", True)
    controller._on_agent_hook(
        _hook(1, "PreToolUse", tool_name="Write", tool_path=f"{wt}/a.py", cwd=main)
    )
    assert controller.displayedRoot == wt  # chrome followed...
    assert controller.agent_start_dir(1) == main  # ...but the pty did not


def test_agent_start_dir_for_vps_slot_stays_local(controller, worktree_env):
    # A vps record's cwd is a path on the SERVER; the process this KSession
    # starts is the local ssh client, so it must chdir locally.
    main, _wt = worktree_env
    _push_cwd(controller, main)
    controller.spawn_agent("fresh", True)
    controller._term_agents[1].update({"location": "vps", "cwd": "/srv/remote/repo"})
    start_dir = controller.agent_start_dir(1)
    assert start_dir == controller.displayedRoot
    # Explicit about the failure being guarded: the remote path must never be
    # handed to a local chdir, and whatever IS returned has to be real — both
    # would be violated if the vps branch moved below the _existing_root call.
    assert start_dir != "/srv/remote/repo"
    assert os.path.isdir(start_dir)


# ---------------------------------------------------------------------------
# QML slot registration — guards against decorator displacement: inserting a
# new method between a @Slot decorator and its def silently re-targets the
# decorator, unregistering the original method from the metaobject. QML then
# sees `undefined` and every callsite throws at runtime while the Python test
# suite (which calls methods directly) stays green. Happened live to
# focus_agent on 2026-07-13 (Ctrl+1..5 went dead).
# ---------------------------------------------------------------------------


def test_qml_facing_slots_are_registered():
    meta = AppController.staticMetaObject
    registered = {
        bytes(meta.method(i).name()).decode() for i in range(meta.methodCount())
    }
    qml_called = {
        "focus_agent",
        "close_agent",
        "spawn_agent",
        "agent_start_dir",
        "anchor_to_path",
        "release_anchor",
        "focus_agent_browser",
        "set_central_surface",
        "cycle_agent_focus",
        "respond_to_permission",
    }
    missing = qml_called - registered
    assert not missing, f"QML-facing methods missing from metaobject: {missing}"
