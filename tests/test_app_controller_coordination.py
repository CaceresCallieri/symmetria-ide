"""AppController wiring tests for agent coordination (wait_for_agent).

Hermetic like test_app_controller_term_agents: no bridge socket, no QML, no
judge subprocess, no event-loop pumping (see the processevents-SEGV memory —
queued deliveries are exercised by calling slots directly, and signal capture
uses direct connections, which are synchronous on the same thread). The pure
trigger/judge logic is covered in test_agent_coordination.py; this file pins
the Qt seams: registration via the MCP body, settle-timer arming, inject
emission + busy-retry, the needs_user attention path, and close-time pruning.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from symmetria_ide import agent_coordination as coord
from symmetria_ide.app import AppController

from test_app_controller_term_agents import FakeBridge


@pytest.fixture
def controller(monkeypatch):
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_PROMPT", raising=False)
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_VIEW", raising=False)
    monkeypatch.setattr(
        "symmetria_ide.app.shutil.which", lambda _name: "/usr/bin/claude"
    )
    c = AppController()
    c._agent_bridge = FakeBridge()
    return c


@pytest.fixture
def two_agents(controller):
    """Slots 1 and 2 spawned (display #1 and #2); watched=1, registrant=2."""
    controller.spawn_agent("fresh", True)
    controller.spawn_agent("fresh", True)
    return controller


def _agent_id(slot: int) -> str:
    return f"{os.getpid()}_{slot}"


def _mark_busy(controller, slot: int) -> None:
    controller._term_agent_activity[slot] = {
        "state": "working",
        "tool": "",
        "agentType": "claude",
    }


def _capture_injects(controller) -> list[dict]:
    captured: list[dict] = []
    controller.agentInjectRequested.connect(
        lambda slot, text, submit, rid: captured.append(
            {"slot": slot, "text": text, "submit": submit, "rid": rid}
        )
    )
    return captured


# ---------------------------------------------------------------------------
# Registration (the wait_for_agent MCP tool body)
# ---------------------------------------------------------------------------


def test_register_resolves_display_to_internal_and_arms(two_agents):
    c = two_agents
    _mark_busy(c, 1)
    reply = c._register_wait_for_mcp(1, _agent_id(2), "api merged")
    assert reply == {"ok": True, "status": "armed", "watched_agent": 1}
    (trigger,) = c._coordination.triggers
    assert trigger.watched_slot == 1
    assert trigger.registrant_slot == 2
    assert trigger.note == "api merged"


def test_register_rejects_bad_display_number(two_agents):
    reply = two_agents._register_wait_for_mcp(7, _agent_id(2), "")
    assert reply["ok"] is False
    assert "no agent #7" in reply["error"]


def test_register_rejects_untagged_or_foreign_caller(two_agents):
    assert two_agents._register_wait_for_mcp(1, "", "")["ok"] is False
    assert two_agents._register_wait_for_mcp(1, "999999_1", "")["ok"] is False


def test_register_rejects_self_wait(two_agents):
    reply = two_agents._register_wait_for_mcp(2, _agent_id(2), "")
    assert reply["ok"] is False


def test_register_already_idle_with_session_evaluates(two_agents):
    """Watched idle + has a session → the judge is kicked off immediately
    (status "evaluating"); the deferred singleShot isn't pumped here — the
    trigger's in-flight mark is the observable contract."""
    c = two_agents
    c._term_agents[1]["session_id"] = "sess-1"
    reply = c._register_wait_for_mcp(1, _agent_id(2), "")
    assert reply["ok"] is True
    assert reply["status"] == "evaluating"
    (trigger,) = c._coordination.triggers
    assert trigger.evaluating


def test_register_opencode_watched_carries_warning(two_agents):
    c = two_agents
    c._term_agents[1]["harness"] = "opencode"
    _mark_busy(c, 1)
    reply = c._register_wait_for_mcp(1, _agent_id(2), "")
    assert reply["ok"] is True
    assert "opencode" not in reply.get("warning", "") or reply["warning"]
    assert "verification" in reply["warning"]


# ---------------------------------------------------------------------------
# Idle-edge settle timer
# ---------------------------------------------------------------------------


def test_idle_edge_arms_settle_timer_and_busy_cancels(two_agents):
    c = two_agents
    _mark_busy(c, 1)
    c._register_wait_for_mcp(1, _agent_id(2), "")
    c._on_coord_idle(1)
    timer = c._coord_settle_timers.get(1)
    assert timer is not None and timer.isActive()
    c._on_coord_busy(1)  # flicker: agent went busy inside the window
    assert 1 not in c._coord_settle_timers


def test_idle_edge_without_watchers_arms_nothing(two_agents):
    two_agents._on_coord_idle(1)
    assert two_agents._coord_settle_timers == {}


# ---------------------------------------------------------------------------
# Firing: opencode caveat, missing session, judge verdicts
# ---------------------------------------------------------------------------


def test_fire_opencode_watched_injects_caveat(two_agents):
    c = two_agents
    c._term_agents[1]["harness"] = "opencode"
    _mark_busy(c, 1)
    c._register_wait_for_mcp(1, _agent_id(2), "note")
    injects = _capture_injects(c)
    c._coord_fire(1)
    assert len(injects) == 1
    assert injects[0]["slot"] == 2
    assert injects[0]["submit"] is True
    assert "opencode" in injects[0]["text"]
    assert c._coordination.triggers == []  # completed, not re-armed


def test_fire_claude_without_session_is_needs_user(two_agents, monkeypatch):
    c = two_agents
    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        c, "_notify_desktop", lambda title, body: notifications.append((title, body))
    )
    _mark_busy(c, 1)
    c._register_wait_for_mcp(1, _agent_id(2), "")
    injects = _capture_injects(c)
    c._term_agents[1]["session_id"] = ""  # never captured
    c._coord_fire(1)
    assert notifications  # user notified
    assert len(injects) == 1 and "hold" in injects[0]["text"].lower()
    # Attention dots on both involved chips (indexed slot-1).
    assert c.agentCoordAttention[0] is True
    assert c.agentCoordAttention[1] is True


def _inflight_trigger(c):
    """Arm + mark a trigger in-flight the way _coord_fire would."""
    (trigger,) = c._coordination.on_agent_idle(1)
    c._coord_inflight[trigger.id] = trigger
    return trigger


def test_verdict_complete_injects_goahead_and_completes(two_agents):
    c = two_agents
    _mark_busy(c, 1)
    c._register_wait_for_mcp(1, _agent_id(2), "api merged")
    c._term_agent_activity.pop(1)
    trigger = _inflight_trigger(c)
    injects = _capture_injects(c)
    c._on_judge_verdict(trigger.id, coord.VERDICT_COMPLETE, "all tests pass")
    assert len(injects) == 1
    assert injects[0]["slot"] == 2
    assert "all tests pass" in injects[0]["text"]
    assert "api merged" in injects[0]["text"]
    assert c._coordination.triggers == []


def test_verdict_in_progress_silently_rearms(two_agents):
    c = two_agents
    _mark_busy(c, 1)
    c._register_wait_for_mcp(1, _agent_id(2), "")
    c._term_agent_activity.pop(1)
    trigger = _inflight_trigger(c)
    injects = _capture_injects(c)
    c._on_judge_verdict(trigger.id, coord.VERDICT_IN_PROGRESS, "mid-task")
    assert injects == []  # no attention spam
    assert c._coordination.triggers == [trigger]
    assert not trigger.evaluating  # armed for the next idle edge


def test_verdict_for_pruned_trigger_is_dropped(two_agents):
    """A late verdict whose trigger was pruned (agent closed mid-judge)
    no-ops — _coord_inflight routing is the guard."""
    c = two_agents
    injects = _capture_injects(c)
    c._on_judge_verdict("no-such-trigger", coord.VERDICT_COMPLETE, "late")
    assert injects == []


# ---------------------------------------------------------------------------
# Inject retry-on-busy (the app-wide single-in-flight QML inject slot)
# ---------------------------------------------------------------------------


def test_coord_inject_busy_retries_then_gives_up(two_agents, monkeypatch):
    c = two_agents
    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        c, "_notify_desktop", lambda title, body: notifications.append((title, body))
    )
    injects = _capture_injects(c)
    c._coord_inject(2, "go ahead")
    rid = injects[0]["rid"]
    assert rid.startswith("coord-")
    # busy → re-registered for retry under the same id, attempts bumped.
    c.agent_inject_done(rid, False, False, "busy")
    assert c._coord_pending_injects[rid]["attempts"] == 2
    # Exhaust the budget: the event must not be lost — dot + notification.
    c._coord_pending_injects[rid]["attempts"] = c._COORD_INJECT_MAX_ATTEMPTS
    c.agent_inject_done(rid, False, False, "busy")
    assert rid not in c._coord_pending_injects
    assert notifications
    assert c.agentCoordAttention[1] is True  # slot 2, indexed slot-1


def test_coord_inject_ok_clears_pending(two_agents):
    c = two_agents
    injects = _capture_injects(c)
    c._coord_inject(2, "go ahead")
    rid = injects[0]["rid"]
    c.agent_inject_done(rid, True, True, "")
    assert c._coord_pending_injects == {}


def test_coord_inject_strips_escape_bytes(two_agents):
    injects = _capture_injects(two_agents)
    two_agents._coord_inject(2, "evil \x1b[201~ payload")
    assert "\x1b" not in injects[0]["text"]


def test_non_coord_inject_still_routes_to_agent_events(two_agents):
    """STT request ids (not coord-minted) keep flowing to resolve_inject."""
    resolved: list[tuple] = []
    two_agents._agent_events.resolve_inject = lambda *a: resolved.append(a)
    two_agents.agent_inject_done("stt-123", True, True, "")
    assert resolved == [("stt-123", True, True, "")]


# ---------------------------------------------------------------------------
# Judge worker body (run synchronously — the thread is the caller's concern)
# ---------------------------------------------------------------------------


def test_run_judge_parses_cli_output(two_agents, monkeypatch, tmp_path):
    c = two_agents
    transcript = tmp_path / "sess.jsonl"
    transcript.write_text(
        '{"type": "assistant", "message": {"role": "assistant", "content": "done"}}\n'
    )
    monkeypatch.setattr(
        coord, "claude_transcript_path", lambda cwd, sid: str(transcript)
    )
    monkeypatch.setattr(
        "symmetria_ide.app.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(
            a,
            0,
            stdout='{"result": "{\\"status\\": \\"complete\\", '
            '\\"summary\\": \\"clean\\"}"}',
            stderr="",
        ),
    )
    verdicts: list[tuple] = []
    # Direct connection (synchronous) alongside the queued production one.
    c._judgeVerdictReady.connect(lambda *a: verdicts.append(a))
    c._run_judge("trig-1", "/some/cwd", "sess", "")
    assert verdicts == [("trig-1", coord.VERDICT_COMPLETE, "clean")]


def test_run_judge_missing_transcript_is_needs_user(two_agents):
    verdicts: list[tuple] = []
    two_agents._judgeVerdictReady.connect(lambda *a: verdicts.append(a))
    two_agents._run_judge("trig-1", "/nope", "sess-does-not-exist", "")
    assert verdicts[0][1] == coord.VERDICT_NEEDS_USER


# ---------------------------------------------------------------------------
# Pruning + attention lifecycle
# ---------------------------------------------------------------------------


def test_close_watched_agent_injects_cancellation(two_agents):
    c = two_agents
    _mark_busy(c, 1)
    c._register_wait_for_mcp(1, _agent_id(2), "note")
    injects = _capture_injects(c)
    c.close_agent(1)
    assert c._coordination.triggers == []
    assert len(injects) == 1
    assert injects[0]["slot"] == 2
    assert "cancelled" in injects[0]["text"].lower()
    assert "#1" in injects[0]["text"]  # display number captured pre-compaction
    assert c.agentCoordAttention[1] is True


def test_close_registrant_discards_trigger_silently(two_agents):
    c = two_agents
    _mark_busy(c, 1)
    c._register_wait_for_mcp(1, _agent_id(2), "")
    injects = _capture_injects(c)
    c.close_agent(2)
    assert c._coordination.triggers == []
    assert injects == []


def test_close_agent_drops_inflight_and_pending(two_agents):
    c = two_agents
    _mark_busy(c, 1)
    c._register_wait_for_mcp(1, _agent_id(2), "")
    c._term_agent_activity.pop(1)
    trigger = _inflight_trigger(c)
    c._coord_pending_injects["coord-x"] = {"slot": 2, "text": "t", "attempts": 1}
    c.close_agent(2)
    assert trigger.id not in c._coord_inflight
    assert "coord-x" not in c._coord_pending_injects


def test_focus_agent_clears_coord_attention(two_agents):
    c = two_agents
    c._agent_coord_attention[2] = "look here"
    c.focus_agent(2)
    assert c.agentCoordAttention[1] is False
