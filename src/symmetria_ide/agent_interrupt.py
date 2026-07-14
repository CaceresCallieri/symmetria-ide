"""Escape-keystroke interrupt detection — the non-hook fallback edge.

Claude Code (verified empirically on 2.1.170) fires NO hook when the user
interrupts a turn with Escape — not ``Stop``, not ``PostToolUseFailure``,
nothing — whether the interrupt lands mid-thinking (model streaming) or
mid-tool (a tool genuinely running). Proven with a PTY harness: the only
events around an interrupt are ``UserPromptSubmit`` (and ``PreToolUse`` for
the tool case) BEFORE the Esc, then silence. (The
``PostToolUseFailure(is_interrupt=True)`` branch in ``agent_activity.py`` is
consequently dead on this version — kept only as forward/older-version
compatibility.)

``AgentActivityMachine`` is event-sourced, so with no terminating event it
holds the slot's last ``thinking``/``working`` state forever and the chip's
sparkle lies about a quiet agent. This module supplies the missing edge from
the one signal the IDE actually has: the Escape keystroke itself.

``EscapeWatcher`` is an event filter installed on each TOP-LEVEL WINDOW
(⚠ NEVER on the ``QGuiApplication`` — an app-level Python filter makes PySide
wrap every QObject any event targets, and wrapping QtWebEngine's ephemeral
mid-destruction internals SEGVs the GUI thread; coredump 2026-07-13, see the
install site in app.py) that OBSERVES — never consumes — ``Key_Escape``
presses and notifies the controller via a callback. It is deliberately dumb: every
"does this Esc mean interrupt?" decision lives in the controller, where the
per-slot state is (mirrors the reporter-vs-``agent_activity`` split). The
filter MUST return ``False`` so the keystroke still reaches the terminal and
performs the actual interrupt — eating it would break the very gesture we're
reacting to.

The controller acts on the observed Esc only when
``should_arm_interrupt_clear`` holds (agent surface visible, a focused agent,
an interruptible state) and then DEFERS the clear behind a short grace window
that any genuine subsequent hook event cancels — so an Esc pressed while the
agent is really still working (e.g. to dismiss an autocomplete) self-corrects
the moment the next real event arrives.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, Qt

__all__ = [
    "INTERRUPT_CLEAR_GRACE_MS",
    "INTERRUPTIBLE_STATES",
    "EscapeWatcher",
    "should_arm_interrupt_clear",
]

# Delay between observing the interrupting Escape and clearing the sparkle.
# Long enough that a genuine subsequent hook (the agent was actually still
# working) arrives first and cancels the clear; short enough that a real
# interrupt feels resolved promptly. Tuned by hand — see the self-correction
# note in the module docstring.
INTERRUPT_CLEAR_GRACE_MS = 1500

# Activity states for which a bare Escape unambiguously means "interrupt the
# running turn". A turn is in flight only while the agent is thinking (model
# generating) or working (tool running); those are exactly the states that get
# stuck after an interrupt. Deliberately EXCLUDES:
#   - "starting"/"clearing" — transient session-lifecycle blips, no turn to interrupt;
#   - "needs_permission"     — the user answers a permission prompt in-terminal, and
#                              Esc there is a denial gesture whose outcome a real hook
#                              (continue) or the absence of one (the next Esc-interrupt)
#                              already covers — arming here would risk clearing a slot
#                              that's legitimately blocked waiting on the user.
INTERRUPTIBLE_STATES = ("thinking", "working")


def should_arm_interrupt_clear(
    *, central_surface: str, focused_slot: int, activity_state: str | None
) -> bool:
    """Pure gate: should an observed Escape arm an interrupt-clear?

    Split out from the controller so the gating is unit-testable without Qt.

    - ``central_surface == "agent"`` is the load-bearing safety gate: the editor
      (nvim) and shell panes receive Escape constantly (vim insert-mode exit,
      shell line-clear), and only the visible agent pane can actually receive
      the user's interrupt, so restricting to the agent surface keeps those
      unrelated Escapes from ever touching agent activity.
    - a focused slot must exist (``!= 0``; 0 means an empty pool).
    - the slot must be mid-turn (``INTERRUPTIBLE_STATES``); an idle slot has no
      activity entry, so ``activity_state`` is ``None`` and this returns False.
    """
    return (
        central_surface == "agent"
        and focused_slot != 0
        and activity_state in INTERRUPTIBLE_STATES
    )


class EscapeWatcher(QObject):
    """Window-level event filter that reports (never consumes) Escape presses.

    Install on each top-level window — NEVER on the ``QGuiApplication`` (see
    the module docstring: app-level install is the 2026-07-13 SEGV).
    ``on_escape`` is invoked on the GUI thread for every ``Key_Escape``
    ``KeyPress`` dispatched to a filtered window;
    the controller decides whether it matters. ``eventFilter`` always returns
    ``False`` — the terminal still needs the Escape to perform the interrupt.
    """

    def __init__(self, on_escape: Callable[[], None], parent: QObject | None = None):
        super().__init__(parent)
        self._on_escape = on_escape

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt override)
        # Cheap early-out: a window-level filter still sees every event the
        # window dispatches, so bail on anything that isn't a key press before
        # touching key data.
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self._on_escape()
        # NEVER consume — returning True here would swallow the Escape and break
        # the actual interrupt we're trying to observe.
        return False
