"""AppController wiring tests for the Alt+M model picker (open_model_picker).

Hermetic like test_app_controller_coordination: no bridge socket, no QML, no
event-loop pumping (see the processevents-SEGV memory — the busy-retry timer is
asserted by inspecting re-registration, never by pumping). The feature injects
Claude Code's own `/model` slash command into the focused agent's pane, reusing
the QML inject path with a `slash-` request-id namespace so completion resolves
on _slash_inject_result rather than the coordination attention-dot tail or the
STT Future.
"""

from __future__ import annotations

import pytest

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
def one_agent(controller):
    """A single claude agent spawned into slot 1 and focused."""
    controller.spawn_agent("fresh", True)
    controller._focused_term_agent = 1
    return controller


def _capture_injects(controller) -> list[dict]:
    captured: list[dict] = []
    controller.agentInjectRequested.connect(
        lambda slot, text, submit, rid: captured.append(
            {"slot": slot, "text": text, "submit": submit, "rid": rid}
        )
    )
    return captured


# ---------------------------------------------------------------------------
# open_model_picker: the Alt+M entry point
# ---------------------------------------------------------------------------


def test_open_model_picker_injects_slash_model_and_focuses(one_agent):
    c = one_agent
    injects = _capture_injects(c)
    c.open_model_picker()

    assert len(injects) == 1
    ev = injects[0]
    assert ev["slot"] == 1
    assert ev["text"] == "/model"
    assert ev["submit"] is True  # auto-submit so the picker actually opens
    assert ev["rid"].startswith("slash-")
    # Registered in the slash namespace (not coord / STT).
    assert ev["rid"] in c._slash_pending_injects
    assert c._slash_pending_injects[ev["rid"]]["attempts"] == 1
    # Surface brought forward so the picker is visible.
    assert c.centralSurface == "agent"


def test_open_model_picker_no_focused_agent_is_noop(controller):
    c = controller
    injects = _capture_injects(c)
    c._focused_term_agent = 0  # nothing focused
    c.open_model_picker()
    assert injects == []
    assert c._slash_pending_injects == {}


def test_open_model_picker_opencode_harness_is_noop(one_agent):
    c = one_agent
    c._term_agents[1]["harness"] = "opencode"  # no /model slash command
    injects = _capture_injects(c)
    c.open_model_picker()
    assert injects == []
    assert c._slash_pending_injects == {}


def test_inject_slash_command_strips_esc(one_agent):
    """An embedded ESC would terminate the bracketed paste early — stripped
    defensively, same as the coordination + STT inject paths."""
    c = one_agent
    injects = _capture_injects(c)
    c._inject_slash_command(1, "/model\x1b[201~evil")
    assert injects[0]["text"] == "/model[201~evil"  # ESC byte removed


# ---------------------------------------------------------------------------
# Completion routing + busy-retry (agent_inject_done -> _slash_inject_result)
# ---------------------------------------------------------------------------


def test_slash_success_clears_pending(one_agent):
    c = one_agent
    c.open_model_picker()
    (rid,) = c._slash_pending_injects
    c.agent_inject_done(rid, True, True, "")
    assert rid not in c._slash_pending_injects


def test_slash_busy_reregisters_for_retry(one_agent):
    c = one_agent
    c.open_model_picker()
    (rid,) = c._slash_pending_injects
    c.agent_inject_done(rid, False, False, "busy")
    # Re-registered under the SAME id with an incremented attempt count; the
    # retry itself fires from a QTimer we deliberately do not pump.
    assert rid in c._slash_pending_injects
    assert c._slash_pending_injects[rid]["attempts"] == 2


def test_slash_exhaustion_logs_without_attention_dot(one_agent):
    """A dropped /model picker is benign — unlike coordination, exhaustion must
    NOT light the attention dot or fire a desktop notification."""
    c = one_agent
    c.open_model_picker()
    (rid,) = c._slash_pending_injects
    # Force the attempt counter to the cap so the next busy result exhausts.
    c._slash_pending_injects[rid]["attempts"] = c._COORD_INJECT_MAX_ATTEMPTS
    c.agent_inject_done(rid, False, False, "busy")
    assert rid not in c._slash_pending_injects  # dropped, not re-registered
    assert c._agent_coord_attention == {}  # no dot lit (coord-only behaviour)


def test_slash_done_does_not_touch_coord_or_stt(one_agent):
    """A slash rid must resolve on _slash_inject_result — never fall through to
    the coordination handler or the STT Future resolver."""
    c = one_agent
    c.open_model_picker()
    (rid,) = c._slash_pending_injects
    # An unrelated coord pending must be untouched by the slash resolution.
    c._coord_pending_injects["coord-x"] = {"slot": 1, "text": "hi", "attempts": 1}
    c.agent_inject_done(rid, True, True, "")
    assert "coord-x" in c._coord_pending_injects


# ---------------------------------------------------------------------------
# Close-time pruning
# ---------------------------------------------------------------------------


def test_slash_pending_pruned_on_agent_close(one_agent):
    c = one_agent
    c.open_model_picker()
    (rid,) = c._slash_pending_injects
    c._on_coord_agent_closed(1)  # slot 1's pane is going away
    assert rid not in c._slash_pending_injects


def test_slash_close_during_retry_cancels_via_inplace_prune(one_agent):
    """Close-during-retry cancellation: a busy result re-registers the inject
    and schedules a QTimer whose closure captured the registry OBJECT. The
    close-time prune MUST mutate that same object in place — a rebind would
    orphan the closure on the old dict and let the retry fire a stray inject
    into the freed (possibly-reused) slot. Asserts both the id is gone AND the
    object identity is preserved, so a regression back to rebind-pruning fails
    here."""
    c = one_agent
    c.open_model_picker()
    (rid,) = c._slash_pending_injects
    captured = c._slash_pending_injects  # the object the retry closure holds
    c.agent_inject_done(rid, False, False, "busy")  # -> re-registered, retry armed
    assert captured is c._slash_pending_injects  # not rebound by the retry
    assert rid in captured
    c._on_coord_agent_closed(1)  # agent closes mid-retry-window
    assert rid not in captured  # closure will find it gone -> no stray emit
    assert captured is c._slash_pending_injects  # pruned in place, not rebound
