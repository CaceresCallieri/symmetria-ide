"""Terminal-agent pool tests (the IDE-native orchestrator runtime).

Hermetic: no bridge socket, no QML, no subprocesses. The real
AgentBridgeClient is constructed but never started; publish assertions go
through a fake swapped into `controller._agent_bridge` after construction
(`_on_bridge_snapshot` is exercised by direct invocation, mirroring the
queued delivery path).
"""

from __future__ import annotations

import os

import pytest

from symmetria_ide.app import AppController


class FakeBridge:
    """Captures the publish API surface of AgentBridgeClient."""

    def __init__(self) -> None:
        self.spawns: list[dict] = []
        self.removes: list[int] = []
        self.focuses: list[int] = []
        self.titles: list[tuple[int, str]] = []
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
    assert argv == [
        "env",
        f"SYMMETRIA_AGENT_ID={os.getpid()}_1",
        "claude",
        "--dangerously-skip-permissions",
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
    assert inst["project"] == os.path.basename(controller.displayedRoot)


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
    assert controller.agent_spawn_argv(2)[1].endswith("_2")


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
