"""Esc-interrupt recovery — the non-hook fallback that un-sticks a sparkle.

Claude Code fires NO hook when the user interrupts a turn with Escape (proven
empirically on 2.1.170), so the event-sourced activity machine would otherwise
hold a slot's "thinking"/"working" sparkle forever after a cancel. These tests
cover the four layers of the fix in `agent_interrupt.py` + `AppController`:

  1. `should_arm_interrupt_clear` — the pure gate (no Qt).
  2. `EscapeWatcher` — the filter OBSERVES Escape, never consumes it.
  3. The controller pipeline — arm on Esc, clear after the grace window, and
     cancel the clear when a real hook supersedes it.
  4. The install site — window-level, NEVER app-level (the 2026-07-13 SEGV).

Hermetic, mirroring test_app_controller_term_agents.py: a real AppController
with a FakeBridge swapped in, no QML / sockets / subprocesses. The grace QTimer
is never pumped (gotcha: pumping the shared app runs prior tests' deleteLater →
SEGV, see .claude/memory/.../processevents_shared_app_segv.md) — its timeout
slot `_clear_interrupted_agent` is invoked directly, exactly as the queued
delivery would, the same way the suite exercises `_on_bridge_snapshot`.
"""

from __future__ import annotations

import os

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt
from PySide6.QtGui import QKeyEvent

from symmetria_ide.agent_interrupt import (
    INTERRUPTIBLE_STATES,
    EscapeWatcher,
    should_arm_interrupt_clear,
)
from symmetria_ide.app import AppController


# ---------------------------------------------------------------------------
# Layer 1: the pure gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "surface,slot,state,expected",
    [
        ("agent", 1, "thinking", True),
        ("agent", 1, "working", True),
        # Wrong surface — the editor/terminal Esc must never arm a clear.
        ("editor", 1, "thinking", False),
        ("terminal", 1, "working", False),
        ("git", 1, "thinking", False),
        # No focused agent.
        ("agent", 0, "thinking", False),
        # Idle (no activity entry → state None) and non-interruptible states.
        ("agent", 1, None, False),
        ("agent", 1, "needs_permission", False),
        ("agent", 1, "starting", False),
        ("agent", 1, "clearing", False),
    ],
)
def test_should_arm_predicate(surface, slot, state, expected):
    assert (
        should_arm_interrupt_clear(
            central_surface=surface, focused_slot=slot, activity_state=state
        )
        is expected
    )


def test_interruptible_states_are_the_active_turn_states():
    # Guards the intent: only a turn-in-flight (thinking/working) is clearable.
    assert set(INTERRUPTIBLE_STATES) == {"thinking", "working"}


# ---------------------------------------------------------------------------
# Layer 2: the event filter (real Qt event delivery, no pumping)
# ---------------------------------------------------------------------------


def _send_key(target: QObject, key: int) -> bool:
    """Synchronously dispatch a KeyPress so app-level filters fire. Returns the
    notify() result (False == not accepted/consumed by a filter)."""
    ev = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
    return QCoreApplication.instance().sendEvent(target, ev)


def test_escape_watcher_fires_on_escape_delivered_through_the_app():
    """An Escape dispatched through Qt's notify loop reaches the installed
    filter.

    Filter-mechanics proof — the install target here is the test app purely
    for convenience (a bare QCoreApplication has no windows): the watcher
    genuinely observes a key event dispatched to another object. The
    PRODUCTION install target is the top-level window, never the app — see
    Layer 4 for the install-site contract.
    """
    calls: list[int] = []
    watcher = EscapeWatcher(lambda: calls.append(1))
    app = QCoreApplication.instance()
    app.installEventFilter(watcher)
    try:
        _send_key(QObject(), Qt.Key.Key_Escape)
        assert calls == [1]
    finally:
        app.removeEventFilter(watcher)


def test_escape_watcher_never_consumes_and_is_selective():
    """`eventFilter` returns False for everything (never eats the key), and only
    fires the callback for an Escape KeyPress."""
    calls: list[int] = []
    watcher = EscapeWatcher(lambda: calls.append(1))
    target = QObject()
    esc = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier
    )
    other = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier
    )
    non_key = QEvent(QEvent.Type.User)

    assert watcher.eventFilter(target, esc) is False  # must not consume the Esc
    assert watcher.eventFilter(target, other) is False
    assert watcher.eventFilter(target, non_key) is False
    # Callback fired exactly once — only for the Escape.
    assert calls == [1]


# ---------------------------------------------------------------------------
# Layer 3: the controller pipeline
# ---------------------------------------------------------------------------


class FakeBridge:
    """Captures notify_activity (the only publish path the clear touches)."""

    def __init__(self) -> None:
        self.activities: list[dict] = []

    def notify_activity(self, slot, *, state, tool, in_plan_mode, session_id=""):
        self.activities.append(
            {
                "slot": slot,
                "state": state,
                "tool": tool,
                "in_plan_mode": in_plan_mode,
                "session_id": session_id,
            }
        )

    # Unused-by-these-tests publish verbs — present so spawn/close don't blow up.
    def notify_spawn(self, instance):  # noqa: D401
        pass

    def notify_remove(self, slot):
        pass

    def notify_focus(self, slot):
        pass

    def notify_title(self, slot, title):
        pass

    def start(self):
        pass

    def stop(self):
        pass


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


def _hook(controller, slot: int, event_name: str, **extra) -> None:
    """Drive one reporter hook event through the real `_on_agent_hook` path."""
    payload = {
        "agent_id": f"{os.getpid()}_{slot}",
        "hook_event_name": event_name,
        "session_id": extra.pop("session_id", "sess-1"),
        "tool_name": extra.pop("tool_name", ""),
        "permission_mode": extra.pop("permission_mode", "default"),
        "event_ts_ns": extra.pop("event_ts_ns", 1),
    }
    payload.update(extra)
    controller._on_agent_hook(payload)


def _make_thinking_focused_agent(controller) -> int:
    """Spawn slot 1, focus it on the agent surface, drive it to 'thinking'."""
    controller.spawn_agent("fresh", True)
    slot = 1
    controller._focused_term_agent = slot
    controller.set_central_surface("agent")
    _hook(controller, slot, "UserPromptSubmit")
    assert controller._term_agent_activity[slot]["state"] == "thinking"
    return slot


def test_escape_arms_clear_when_focused_agent_is_thinking(controller):
    slot = _make_thinking_focused_agent(controller)
    controller.on_terminal_escape()
    assert slot in controller._pending_interrupt_clears
    assert controller._pending_interrupt_clears[slot].isActive()


def test_grace_clear_returns_sparkle_to_idle_and_publishes(controller):
    slot = _make_thinking_focused_agent(controller)
    controller.on_terminal_escape()
    controller._agent_bridge.activities.clear()  # ignore the hook's own publish

    # Fire the timeout slot directly (the QTimer would call exactly this).
    controller._clear_interrupted_agent(slot)

    # Activity entry popped → the chip falls back to its dormant dot.
    assert slot not in controller._term_agent_activity
    assert controller.agentActivity[slot - 1]["state"] == ""
    # The now-idle state was published outward (state="") with session retained.
    assert controller._agent_bridge.activities == [
        {
            "slot": slot,
            "state": "",
            "tool": "",
            "in_plan_mode": False,
            "session_id": "sess-1",
        }
    ]
    # Timer record cleaned up.
    assert slot not in controller._pending_interrupt_clears


def test_real_hook_cancels_pending_clear(controller):
    slot = _make_thinking_focused_agent(controller)
    controller.on_terminal_escape()
    assert slot in controller._pending_interrupt_clears

    # A genuine subsequent event (agent was still working) supersedes the clear.
    _hook(controller, slot, "PreToolUse", tool_name="Bash")

    assert slot not in controller._pending_interrupt_clears
    assert controller._term_agent_activity[slot]["state"] == "working"


def test_escape_off_agent_surface_does_not_arm(controller):
    slot = _make_thinking_focused_agent(controller)
    controller.set_central_surface("editor")  # user looking at the editor (vim Esc)
    controller.on_terminal_escape()
    assert slot not in controller._pending_interrupt_clears
    # Sparkle untouched.
    assert controller._term_agent_activity[slot]["state"] == "thinking"


def test_escape_suppressed_while_modal_overlay_open(controller):
    # An open input-capturing overlay (spawn menu, picker, dialog, fuzzy finder)
    # consumes the Escape to close itself — it never reaches the agent terminal,
    # so it is not an interrupt and must not arm a clear.
    slot = _make_thinking_focused_agent(controller)
    controller.set_modal_overlay_open(True)
    controller.on_terminal_escape()
    assert slot not in controller._pending_interrupt_clears
    assert controller._term_agent_activity[slot]["state"] == "thinking"
    # Once the overlay closes, a real interrupt Escape arms again.
    controller.set_modal_overlay_open(False)
    controller.on_terminal_escape()
    assert slot in controller._pending_interrupt_clears


def test_escape_when_idle_does_not_arm(controller):
    controller.spawn_agent("fresh", True)
    slot = 1
    controller._focused_term_agent = slot
    controller.set_central_surface("agent")
    # No activity entry → idle.
    assert slot not in controller._term_agent_activity
    controller.on_terminal_escape()
    assert slot not in controller._pending_interrupt_clears


def test_escape_when_needs_permission_does_not_arm(controller):
    slot = _make_thinking_focused_agent(controller)
    _hook(controller, slot, "PermissionRequest", tool_name="Bash")
    assert controller._term_agent_activity[slot]["state"] == "needs_permission"
    controller.on_terminal_escape()
    assert slot not in controller._pending_interrupt_clears


def test_close_agent_cancels_pending_clear(controller):
    slot = _make_thinking_focused_agent(controller)
    controller.on_terminal_escape()
    assert slot in controller._pending_interrupt_clears
    controller.close_agent(slot)
    assert slot not in controller._pending_interrupt_clears
    # A stray timeout after close is a no-op (slot gone), never a crash.
    controller._clear_interrupted_agent(slot)


def test_clear_no_op_when_already_cleared_by_real_event(controller):
    slot = _make_thinking_focused_agent(controller)
    controller.on_terminal_escape()
    # A real Stop clears the slot AND cancels the timer first.
    _hook(controller, slot, "Stop")
    assert slot not in controller._term_agent_activity
    controller._agent_bridge.activities.clear()
    # A late stray timeout must not re-publish or crash.
    controller._clear_interrupted_agent(slot)
    assert controller._agent_bridge.activities == []


# ---------------------------------------------------------------------------
# Layer 4: the install site — window-level, NEVER app-level
# ---------------------------------------------------------------------------


def test_install_escape_watcher_targets_windows_not_the_app(controller, monkeypatch):
    """The production install goes on each top-level window and NEVER on the
    QGuiApplication — the app-level install is the 2026-07-13 SEGV (PySide
    wraps every event-target QObject, including QtWebEngine's ephemeral
    mid-destruction internals; see _install_escape_watcher). This is the
    regression tripwire: a future "simplification" back to
    app.installEventFilter would make the unrelated-object assertion fail."""
    import symmetria_ide.app as app_module

    fake_window = QObject()
    monkeypatch.setattr(
        app_module.QGuiApplication,
        "topLevelWindows",
        staticmethod(lambda: [fake_window]),
    )
    calls: list[int] = []
    monkeypatch.setattr(controller, "on_terminal_escape", lambda: calls.append(1))
    controller._install_escape_watcher()

    # An Escape delivered to the filtered window fires the callback…
    _send_key(fake_window, Qt.Key.Key_Escape)
    assert calls == [1]
    # …but one delivered to an unrelated object does NOT — an app-level
    # filter would have observed this dispatch too.
    _send_key(QObject(), Qt.Key.Key_Escape)
    assert calls == [1]


def test_install_escape_watcher_no_window_skips_never_falls_back(
    controller, monkeypatch
):
    """Headless (no top-level window): the install is skipped gracefully —
    no crash, and crucially NO fallback to an app-level filter."""
    import symmetria_ide.app as app_module

    monkeypatch.setattr(
        app_module.QGuiApplication, "topLevelWindows", staticmethod(lambda: [])
    )
    calls: list[int] = []
    monkeypatch.setattr(controller, "on_terminal_escape", lambda: calls.append(1))
    controller._install_escape_watcher()

    _send_key(QObject(), Qt.Key.Key_Escape)
    assert calls == []
