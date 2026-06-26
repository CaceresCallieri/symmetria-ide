"""Pure unit tests for the IDE-local activity state machine (no Qt imports).

`AgentActivityMachine` consolidates the event→state mapping (formerly in the
shell's symmetria-agent-hook.py) and the subagent-depth / idle-pop logic
(formerly in the shell bridge). These tests pin that contract so the AgentTopBar
sparkle vocabulary stays byte-identical to the legacy bridge path.
"""

from __future__ import annotations

from symmetria_ide.agent_activity import AgentActivityMachine


def _event(hook_event: str, *, agent_id: str = "100_1", **fields) -> dict:
    """Build the curated hook dict the reporter forwards."""
    return {"agent_id": agent_id, "hook_event_name": hook_event, **fields}


# ---------------------------------------------------------------------------
# Basic event → state mapping
# ---------------------------------------------------------------------------


def test_user_prompt_submit_is_thinking():
    out = AgentActivityMachine().apply(_event("UserPromptSubmit"))
    assert out.state == "thinking"
    assert out.clear is False


def test_pre_tool_use_is_working_with_tool_label():
    out = AgentActivityMachine().apply(_event("PreToolUse", tool_name="Bash"))
    assert out.state == "working"
    assert out.tool == "Running"


def test_unknown_tool_name_falls_back_to_raw():
    out = AgentActivityMachine().apply(_event("PreToolUse", tool_name="Frobnicate"))
    assert out.tool == "Frobnicate"


def test_permission_request_is_needs_permission():
    out = AgentActivityMachine().apply(_event("PermissionRequest", tool_name="Write"))
    assert out.state == "needs_permission"
    assert out.tool == "Writing"


# ---------------------------------------------------------------------------
# Clear states (idle / offline pop the activity entry)
# ---------------------------------------------------------------------------


def test_stop_clears_activity():
    out = AgentActivityMachine().apply(_event("Stop"))
    assert out.clear is True
    assert out.state == ""


def test_session_end_clears_activity():
    out = AgentActivityMachine().apply(_event("SessionEnd"))
    assert out.clear is True


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


def test_clear_sourced_session_start_is_clearing():
    out = AgentActivityMachine().apply(_event("SessionStart", source="clear"))
    assert out.state == "clearing"


def test_normal_session_start_is_starting():
    out = AgentActivityMachine().apply(_event("SessionStart", source="startup"))
    assert out.state == "starting"


def test_idle_notification_promotes_observer_to_idle():
    # A bare Notification is observer-only (no-op); the idle marker promotes it.
    machine = AgentActivityMachine()
    assert machine.apply(_event("Notification")).clear is False
    assert machine.apply(_event("Notification")).state == ""
    out = machine.apply(_event("Notification", idle_notification=True))
    assert out.clear is True


def test_post_tool_use_failure_interrupt_maps_to_idle():
    out = AgentActivityMachine().apply(_event("PostToolUseFailure", is_interrupt=True))
    assert out.clear is True
    # Without the interrupt flag it is the normal "thinking".
    out2 = AgentActivityMachine().apply(_event("PostToolUseFailure"))
    assert out2.state == "thinking"


# ---------------------------------------------------------------------------
# Observer-only / unmapped events are no-ops
# ---------------------------------------------------------------------------


def test_observer_only_event_is_noop():
    out = AgentActivityMachine().apply(_event("FileChanged"))
    assert out.state == ""
    assert out.clear is False


def test_unmapped_event_is_noop():
    out = AgentActivityMachine().apply(_event("BrandNewEventType2027"))
    assert out.state == ""
    assert out.clear is False


# ---------------------------------------------------------------------------
# Subagent depth + recap drop
# ---------------------------------------------------------------------------


def test_subagent_start_is_working_delegating():
    out = AgentActivityMachine().apply(_event("SubagentStart"))
    assert out.state == "working"
    assert out.tool == "Delegating"


def test_paired_subagent_stop_is_thinking():
    machine = AgentActivityMachine()
    machine.apply(_event("SubagentStart"))
    out = machine.apply(_event("SubagentStop"))
    assert out.state == "thinking"


def test_unpaired_subagent_stop_is_recap_drop():
    # No preceding SubagentStart → the recap echo → no-op (does not resurrect
    # a cleared slot).
    out = AgentActivityMachine().apply(_event("SubagentStop"))
    assert out.state == ""
    assert out.clear is False


def test_nested_subagents_balance():
    machine = AgentActivityMachine()
    machine.apply(_event("SubagentStart"))
    machine.apply(_event("SubagentStart"))
    assert machine.apply(_event("SubagentStop")).state == "thinking"  # depth 2→1
    assert machine.apply(_event("SubagentStop")).state == "thinking"  # depth 1→0
    # A third stop is now unpaired → recap drop.
    assert machine.apply(_event("SubagentStop")).state == ""


def test_depth_is_per_agent():
    machine = AgentActivityMachine()
    machine.apply(_event("SubagentStart", agent_id="100_1"))
    # A different agent's stop is unpaired (its own depth is 0).
    assert machine.apply(_event("SubagentStop", agent_id="100_2")).state == ""
    # The first agent's stop is still paired.
    assert machine.apply(_event("SubagentStop", agent_id="100_1")).state == "thinking"


def test_forget_resets_depth():
    machine = AgentActivityMachine()
    machine.apply(_event("SubagentStart"))
    machine.forget("100_1")
    # After forget, the pending start is gone → this stop is unpaired.
    assert machine.apply(_event("SubagentStop")).state == ""


# ---------------------------------------------------------------------------
# session_id capture (orthogonal to activity shape)
# ---------------------------------------------------------------------------


def test_session_id_captured_on_active_event():
    out = AgentActivityMachine().apply(_event("UserPromptSubmit", session_id="abc-123"))
    assert out.session_id == "abc-123"


def test_session_id_captured_on_clear_event():
    # Stop clears activity but still carries the session id for restore capture.
    out = AgentActivityMachine().apply(_event("Stop", session_id="abc-123"))
    assert out.session_id == "abc-123"
    assert out.clear is True


def test_session_id_captured_on_observer_event():
    out = AgentActivityMachine().apply(_event("FileChanged", session_id="abc-123"))
    assert out.session_id == "abc-123"


def test_missing_session_id_is_empty_string():
    out = AgentActivityMachine().apply(_event("UserPromptSubmit"))
    assert out.session_id == ""


# ---------------------------------------------------------------------------
# Plan mode + ordering
# ---------------------------------------------------------------------------


def test_plan_mode_from_permission_mode():
    out = AgentActivityMachine().apply(
        _event("UserPromptSubmit", permission_mode="plan")
    )
    assert out.in_plan_mode is True
    out2 = AgentActivityMachine().apply(
        _event("UserPromptSubmit", permission_mode="default")
    )
    assert out2.in_plan_mode is False


def test_out_of_order_event_does_not_raise():
    machine = AgentActivityMachine()
    machine.apply(_event("PreToolUse", tool_name="Bash", event_ts_ns=2000))
    # An earlier-timestamped event arriving late is logged, not dropped.
    out = machine.apply(_event("PostToolUse", event_ts_ns=1000))
    assert out.state == "thinking"
