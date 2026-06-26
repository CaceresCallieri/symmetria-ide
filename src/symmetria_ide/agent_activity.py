"""Agent activity state machine — the IDE-local interpretation of hook events.

Part of the agent-ownership inversion (`docs/agent-ownership-inversion.md`):
the IDE captures its OWN agents' activity locally instead of round-tripping
through Symmetria Shell's `agent-bridge.py`. This module is the brain of that
capture — it turns one raw Claude-Code hook event (forwarded verbatim by
`runtime/symmetria-ide-agent-hook.py` over the IDE's agent socket) into an
``ActivityOutcome`` the controller applies to a pool slot.

It CONSOLIDATES logic that today lives in two separate processes:

  - **event → state mapping** (`EVENT_STATE_MAP`, `TOOL_DISPLAY_NAMES`, the
    clearing / idle-notification / interrupt overrides) — ported from the shell's
    `symmetria-agent-hook.py`, which used to do this mapping inside the hook
    before sending an already-cooked ``state`` to the bridge;
  - **subagent-depth tracking + idle-pop + out-of-order detection** — ported
    from the shell bridge's ``handle_message`` activity branch, which held the
    per-agent memory the hook (one-shot process) could not.

Collapsing both halves into one place is the whole point: the reporter becomes a
dumb relay, and the entire interpretation is here — pure (no Qt, no I/O) and
unit-testable, exactly like `agent_harness.py`. The per-agent state (subagent
depth, last-seen timestamp) lives on the long-running ``AgentActivityMachine``
instance the controller owns.

The produced ``state`` strings (``starting``/``clearing``/``thinking``/
``working``/``needs_permission``) are the SAME vocabulary the shell hook emitted,
so the AgentTopBar sparkle visuals (which read ``activity.state``) render
identically whether activity arrives via this local path or the legacy bridge
snapshot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Hook event name → activity state. Ported verbatim from the shell's
# symmetria-agent-hook.py so the produced vocabulary is byte-identical. Coverage
# is intentionally generous (Claude Code keeps adding events): an explicit "" maps
# an OBSERVER-ONLY event — known, but carrying no lifecycle transition — which we
# silently ignore, versus an event ABSENT from this map, which we log as unmapped
# so a genuinely new lifecycle signal surfaces rather than being dropped quietly.
EVENT_STATE_MAP: dict[str, str] = {
    # Session lifecycle
    "SessionStart": "starting",
    "SessionEnd": "offline",
    # Per-turn input
    "UserPromptSubmit": "thinking",
    "UserPromptExpansion": "thinking",
    # Per-turn output
    "Stop": "idle",
    "StopFailure": "idle",
    # Tool use
    "PreToolUse": "working",
    "PostToolUse": "thinking",
    "PostToolUseFailure": "thinking",
    "PostToolBatch": "thinking",
    "PermissionRequest": "needs_permission",
    "PermissionDenied": "thinking",
    # Subagents
    "SubagentStart": "working",
    "SubagentStop": "thinking",
    # Context management
    "PreCompact": "thinking",
    "PostCompact": "thinking",
    # Observer-only (explicitly mapped to "" to suppress the unmapped-event log;
    # carry no lifecycle transition). EXCEPTION: a Notification carrying the
    # idle-notification argv marker is promoted to "idle" below.
    "Notification": "",
    "TaskCreated": "",
    "TaskCompleted": "",
    "TeammateIdle": "",
    "FileChanged": "",
    "CwdChanged": "",
    "InstructionsLoaded": "",
    "ConfigChange": "",
    "Elicitation": "",
    "ElicitationResult": "",
    "WorktreeCreate": "",
    "WorktreeRemove": "",
}

# Tool name → human-readable verb shown on the chip while a tool runs. Ported
# from the shell hook; an unknown tool falls back to its raw name.
TOOL_DISPLAY_NAMES: dict[str, str] = {
    "Edit": "Editing",
    "Write": "Writing",
    "Read": "Reading",
    "Bash": "Running",
    "Glob": "Searching",
    "Grep": "Searching",
    "Task": "Delegating",
    "WebFetch": "Fetching",
    "UrlFetch": "Fetching",
    "WebSearch": "Searching",
    "NotebookEdit": "Editing",
    "AskUserQuestion": "Asking",
    "ExitPlanMode": "Planning",
    "TodoWrite": "Organizing",
    "TaskCreate": "Organizing",
    "TaskUpdate": "Organizing",
    "TaskList": "Organizing",
    "TaskGet": "Organizing",
}

# States that END an agent's active period — the activity entry is popped (the
# chip falls back to its dormant dot) rather than set, so a stale "working"
# never lingers after the agent goes quiet.
_CLEAR_STATES = ("offline", "idle")


@dataclass(frozen=True)
class ActivityOutcome:
    """The controller-facing result of interpreting one hook event.

    Three shapes the controller acts on:

      - **set activity**: ``state`` non-empty, ``clear`` False → write
        ``{state, tool, in_plan_mode}`` to the slot's activity.
      - **clear activity**: ``clear`` True (idle/offline) → pop the slot's
        activity entry.
      - **no-op**: ``state`` empty and ``clear`` False (observer-only event,
        recap echo, or unmapped event) → leave activity untouched.

    ``session_id`` is ORTHOGONAL to the activity shape: it is captured on every
    event that carries one (including no-ops and clears) so the controller can
    backfill the slot's resumable session id for restore. "" means this event
    carried none — the controller keeps whatever it had (sticky semantics).
    """

    session_id: str
    state: str
    tool: str
    in_plan_mode: bool
    clear: bool


class AgentActivityMachine:
    """Per-agent activity interpreter — holds the cross-event memory.

    One instance lives on the controller for the IDE's whole run; ``apply`` is
    called from the GUI thread (the controller marshals socket events there
    before calling). The only per-agent state is the subagent-nesting depth and
    the last-seen event timestamp (for out-of-order logging) — both keyed by the
    ``<ide_pid>_<slot>`` agent id and dropped via ``forget`` when a slot closes.
    """

    def __init__(self) -> None:
        # agent_id -> current subagent nesting depth. Claude Code's end-of-turn
        # "recap" is implemented as a background subagent that fires SubagentStop
        # AFTER the parent's Stop already cleared the slot; the depth counter lets
        # us classify an unpaired SubagentStop as a recap echo and drop it instead
        # of resurrecting a cleared "thinking".
        self._subagent_depth: dict[str, int] = {}
        # agent_id -> last event_ts_ns seen. Async hooks race over the socket, so
        # a logically-earlier event can arrive late; we LOG the anomaly (never
        # drop — a late Stop might still be the correct final state) for diagnosis.
        self._last_event_ts_ns: dict[str, int] = {}

    def forget(self, agent_id: str) -> None:
        """Drop a closed agent's per-agent state (called from close_agent)."""
        self._subagent_depth.pop(agent_id, None)
        self._last_event_ts_ns.pop(agent_id, None)

    def apply(self, event: dict) -> ActivityOutcome:
        """Interpret one forwarded hook event into an ``ActivityOutcome``.

        ``event`` is the curated dict the reporter forwards (see
        runtime/symmetria-ide-agent-hook.py): ``agent_id``, ``hook_event_name``,
        ``session_id``, ``tool_name``, ``permission_mode``, ``source``,
        ``is_interrupt``, ``idle_notification``, ``event_ts_ns``.
        """
        agent_id = str(event.get("agent_id", ""))
        session_id = str(event.get("session_id", "") or "")
        hook_event = str(event.get("hook_event_name", ""))
        # permission_mode is on every hook payload, so plan-mode is always current
        # (no accumulation). Computed up front; only surfaced on active outcomes.
        in_plan_mode = event.get("permission_mode", "") == "plan"

        self._check_ordering(
            agent_id, int(event.get("event_ts_ns", 0) or 0), hook_event
        )

        # A no-op outcome that still carries the session id (capture is orthogonal
        # to activity — see ActivityOutcome). Reused for every "don't touch
        # activity" branch below.
        def noop() -> ActivityOutcome:
            return ActivityOutcome(session_id, "", "", in_plan_mode, clear=False)

        if hook_event not in EVENT_STATE_MAP:
            # A genuinely new Claude-Code event — surface it loudly so we classify
            # it deliberately rather than silently dropping a lifecycle signal.
            log.info(
                "agent_activity: unmapped event %r for %s",
                hook_event or "(none)",
                agent_id,
            )
            return noop()

        state = EVENT_STATE_MAP[hook_event]

        # Overrides (must run BEFORE the observer-only check — the Notification
        # case PROMOTES an otherwise-observer event to a real "idle"):
        # (1) a clear-sourced SessionStart drives the blink animation (start→stop)
        #     instead of the eye-open hold of a normal startup/resume.
        if hook_event == "SessionStart" and event.get("source") == "clear":
            state = "clearing"
        # (2) Claude fires NO hook on a user interrupt (Esc); the delayed
        #     Notification(idle_prompt) and a mid-tool PostToolUseFailure with
        #     is_interrupt are the two partial signals, both mapped to idle.
        elif hook_event == "Notification" and event.get("idle_notification"):
            state = "idle"
        elif hook_event == "PostToolUseFailure" and event.get("is_interrupt") is True:
            log.info("agent_activity: interrupt | %s mapped to idle", agent_id)
            state = "idle"

        # Observer-only event with no promotion — capture session id, touch nothing.
        if not state:
            return noop()

        tool = self._tool_label(hook_event, event)

        # Subagent depth: increment on start, decrement on a paired stop, and drop
        # an UNPAIRED stop (the recap echo) without disturbing the cleared slot.
        if hook_event == "SubagentStart":
            self._subagent_depth[agent_id] = self._subagent_depth.get(agent_id, 0) + 1
        elif hook_event == "SubagentStop":
            depth = self._subagent_depth.get(agent_id, 0)
            if depth > 0:
                self._subagent_depth[agent_id] = depth - 1
            else:
                log.info(
                    "agent_activity: RECAP DETECTED | %s SubagentStop without start"
                    " — dropping",
                    agent_id,
                )
                return noop()

        if state in _CLEAR_STATES:
            return ActivityOutcome(session_id, "", "", in_plan_mode, clear=True)
        return ActivityOutcome(session_id, state, tool, in_plan_mode, clear=False)

    # -- internals -------------------------------------------------------

    @staticmethod
    def _tool_label(hook_event: str, event: dict) -> str:
        """Human-readable verb for a tool-bearing event ("" otherwise)."""
        if hook_event in ("PreToolUse", "PermissionRequest"):
            tool_name = str(event.get("tool_name", ""))
            return TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
        if hook_event == "SubagentStart":
            return "Delegating"  # no tool_name in the payload; label directly
        return ""

    def _check_ordering(self, agent_id: str, event_ts_ns: int, hook_event: str) -> None:
        """Log (never drop) an out-of-order event; advance the per-agent baseline.

        Diagnostic only — the controller acts on whatever order events arrive.
        We persist the baseline across idle (unlike the shell, which stored it in
        the popped activity entry) so the logging survives an idle gap.
        """
        prev_ts = self._last_event_ts_ns.get(agent_id, 0)
        if event_ts_ns and prev_ts and event_ts_ns < prev_ts:
            lag_ms = (prev_ts - event_ts_ns) / 1_000_000
            log.warning(
                "agent_activity: OUT-OF-ORDER | %s event=%s arrived %dms after a"
                " newer event",
                agent_id,
                hook_event or "?",
                int(lag_ms),
            )
        if event_ts_ns:
            self._last_event_ts_ns[agent_id] = event_ts_ns
