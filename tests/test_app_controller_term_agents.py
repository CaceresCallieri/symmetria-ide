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
        self.inject_results: list[tuple[str, bool, bool, str]] = []
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

    def send_inject_result(
        self, request_id: str, ok: bool, submitted: bool, error: str = ""
    ) -> None:
        self.inject_results.append((request_id, ok, submitted, error))

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
# Harness selection (claude / opencode)
# ---------------------------------------------------------------------------


def test_spawn_opencode_fresh_dangerous_argv(controller):
    controller.spawn_agent("fresh", True, "opencode")
    assert controller.agent_spawn_argv(1) == [
        "env",
        f"SYMMETRIA_AGENT_ID={os.getpid()}_1",
        'OPENCODE_PERMISSION={"*":{"*":"allow"}}',
        "opencode",
    ]


def test_spawn_opencode_resume_with_session_id_argv(controller):
    controller.spawn_agent("resume", False, "opencode", "ses_abc")
    assert controller.agent_spawn_argv(1)[-2:] == ["--session", "ses_abc"]


def test_spawn_opencode_resume_without_session_id_is_a_no_op(controller):
    # `opencode --session` with no id errors on spawn — the controller
    # refuses the spawn instead (the QML picker supplies the id).
    controller.spawn_agent("resume", True, "opencode")
    assert controller.agentOrder == []


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
# STT indicator mirroring (snapshot "stt" field → sttTargetSlot/sttTranscribing)
# ---------------------------------------------------------------------------


def _stt_snapshot(stt: dict | None, *agents: dict) -> dict:
    # Delegate to the canonical snapshot builder so the two can't drift.
    snap = _snapshot(*agents)
    snap["stt"] = stt
    return snap


def test_stt_targets_explicit_live_slot(controller):
    controller.spawn_agent("fresh", True)
    controller._on_bridge_snapshot(
        _stt_snapshot({"terminal_pid": os.getpid(), "buf": 1, "transcribing": False})
    )
    assert controller.sttTargetSlot == 1
    assert controller.sttTranscribing is False


def test_stt_transcribing_flag_mirrors(controller):
    controller.spawn_agent("fresh", True)
    controller._on_bridge_snapshot(
        _stt_snapshot({"terminal_pid": os.getpid(), "buf": 1, "transcribing": True})
    )
    assert controller.sttTranscribing is True


def test_stt_buf_minus_one_resolves_to_focused_slot(controller):
    controller.spawn_agent("fresh", True)
    controller.spawn_agent("fresh", True)
    controller.focus_agent(2)
    controller._on_bridge_snapshot(
        _stt_snapshot({"terminal_pid": os.getpid(), "buf": -1, "transcribing": False})
    )
    assert controller.sttTargetSlot == 2


def test_stt_buf_minus_one_with_empty_pool_clears(controller):
    # No agents spawned: the focused-slot fallback resolves to 0, so the
    # indicator must stay dark and transcribing must not latch.
    controller._on_bridge_snapshot(
        _stt_snapshot({"terminal_pid": os.getpid(), "buf": -1, "transcribing": True})
    )
    assert controller.sttTargetSlot == 0
    assert controller.sttTranscribing is False


def test_stt_buf_zero_never_resolves(controller):
    controller.spawn_agent("fresh", True)
    controller._on_bridge_snapshot(
        _stt_snapshot({"terminal_pid": os.getpid(), "buf": 0, "transcribing": True})
    )
    assert controller.sttTargetSlot == 0
    assert controller.sttTranscribing is False


def test_stt_foreign_pid_is_ignored(controller):
    controller.spawn_agent("fresh", True)
    controller._on_bridge_snapshot(
        _stt_snapshot({"terminal_pid": 99999, "buf": 1, "transcribing": False})
    )
    assert controller.sttTargetSlot == 0


def test_stt_dead_slot_is_ignored(controller):
    controller.spawn_agent("fresh", True)
    controller._on_bridge_snapshot(
        _stt_snapshot({"terminal_pid": os.getpid(), "buf": 4, "transcribing": True})
    )
    assert controller.sttTargetSlot == 0
    assert controller.sttTranscribing is False


def test_stt_null_clears_previous_target(controller):
    controller.spawn_agent("fresh", True)
    active = _stt_snapshot(
        {"terminal_pid": os.getpid(), "buf": 1, "transcribing": False}
    )
    controller._on_bridge_snapshot(active)
    assert controller.sttTargetSlot == 1
    controller._on_bridge_snapshot(_stt_snapshot(None))
    assert controller.sttTargetSlot == 0


def test_stt_missing_field_is_tolerated(controller):
    # Old hub builds emit snapshots without "stt" — must not raise or latch.
    controller.spawn_agent("fresh", True)
    controller._on_bridge_snapshot(
        _stt_snapshot({"terminal_pid": os.getpid(), "buf": 1, "transcribing": False})
    )
    controller._on_bridge_snapshot(_snapshot())
    assert controller.sttTargetSlot == 0


def test_stt_emits_only_on_change(controller):
    controller.spawn_agent("fresh", True)
    emissions: list[None] = []
    controller.sttStateChanged.connect(lambda: emissions.append(None))
    payload = _stt_snapshot(
        {"terminal_pid": os.getpid(), "buf": 1, "transcribing": False}
    )
    controller._on_bridge_snapshot(payload)
    controller._on_bridge_snapshot(payload)
    assert len(emissions) == 1


def test_stt_malformed_payload_is_ignored(controller):
    controller.spawn_agent("fresh", True)
    controller._on_bridge_snapshot(
        _stt_snapshot({"terminal_pid": "garbage", "buf": "x", "transcribing": True})
    )
    assert controller.sttTargetSlot == 0


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


# ---------------------------------------------------------------------------
# Bridge-mediated STT injection (_on_bridge_inject routing + validation)
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


def test_inject_routes_to_requested_slot(controller, bridge):
    controller.spawn_agent("fresh", True)  # slot 1
    controller.spawn_agent("fresh", True)  # slot 2
    emitted = _capture_inject_emissions(controller)
    controller._on_bridge_inject(
        {"request_id": "r1", "buf": 2, "text": "hola", "submit": True}
    )
    assert emitted == [(2, "hola", True, "r1")]
    assert bridge.inject_results == []  # QML closes the loop, not Python


def test_inject_dead_slot_falls_back_to_focused(controller):
    controller.spawn_agent("fresh", True)  # slot 1, focused
    emitted = _capture_inject_emissions(controller)
    controller._on_bridge_inject(
        {"request_id": "r2", "buf": 4, "text": "hola", "submit": False}
    )
    assert emitted == [(1, "hola", False, "r2")]


def test_inject_with_no_agents_fails_fast(controller, bridge):
    emitted = _capture_inject_emissions(controller)
    controller._on_bridge_inject(
        {"request_id": "r3", "buf": 1, "text": "hola", "submit": True}
    )
    assert emitted == []
    assert bridge.inject_results == [("r3", False, False, "no-agent")]


def test_inject_empty_text_fails_fast(controller, bridge):
    controller.spawn_agent("fresh", True)
    controller._on_bridge_inject({"request_id": "r4", "buf": 1, "text": ""})
    assert bridge.inject_results == [("r4", False, False, "empty-text")]


def test_inject_strips_escape_characters(controller):
    # An embedded ESC could terminate the QML-side bracketed paste early
    # (\x1b[201~) and leak the remainder as live keystrokes.
    controller.spawn_agent("fresh", True)
    emitted = _capture_inject_emissions(controller)
    controller._on_bridge_inject(
        {"request_id": "r5", "buf": 1, "text": "a\x1b[201~rm -rf", "submit": False}
    )
    assert emitted == [(1, "a[201~rm -rf", False, "r5")]


def test_inject_missing_request_id_is_ignored(controller, bridge):
    controller.spawn_agent("fresh", True)
    emitted = _capture_inject_emissions(controller)
    controller._on_bridge_inject({"buf": 1, "text": "hola"})
    assert emitted == []
    assert bridge.inject_results == []


def test_agent_inject_done_relays_to_bridge(controller, bridge):
    controller.agent_inject_done("r6", True, True, "")
    assert bridge.inject_results == [("r6", True, True, "")]
