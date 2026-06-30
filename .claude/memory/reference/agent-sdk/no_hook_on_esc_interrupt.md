---
name: no_hook_on_esc_interrupt
description: "Claude Code fires NO hook on an Esc-interrupt (2.1.170, mid-thinking AND mid-tool) so the IDE's event-sourced sparkle sticks; recovery is the EscapeWatcher fallback in agent_interrupt.py"
metadata: 
  node_type: memory
  type: reference
  originSessionId: 0890aa26-8dc2-44df-a451-06a9b4b18c51
---

**Claude Code (verified empirically on 2.1.170) fires NO lifecycle hook when the user interrupts a turn with Escape** — not `Stop`, not `PostToolUse`, not `PostToolUseFailure`, nothing — whether the interrupt lands **mid-thinking** (model streaming) or **mid-tool** (a tool genuinely running). The IDE's agent sparkle is event-sourced (`agent_activity.py`: `UserPromptSubmit`→thinking, `PreToolUse`→working, `Stop`→idle), so with no terminating event the chip stays stuck "thinking"/"working" forever after a cancel. This is the "agent N looks like it's running when it isn't" bug.

**How it was proven (reusable harness):** a `pexpect` PTY harness spawned a real `claude --settings <hooks-json>` with a logger hook on every event, submitted a prompt, let it stream, then sent `\x1b`. Two scenarios:
- mid-thinking: events were `SessionStart`, `UserPromptSubmit`, then **silence** after Esc (TUI showed `⎿ Interrupted · What should Claude do instead?`).
- mid-tool (`ping -c 30`): `UserPromptSubmit`, `PreToolUse(Bash)`, then **silence** after Esc.

The Claude Code guide confirmed the docs side: `Stop` explicitly does not fire on interrupts; there is no `Idle`/`Cancelled`/`TurnEnd` event; the `Notification` `idle_prompt` matcher is a delayed idle-*timeout* (~tens of seconds, focus-dependent), not an immediate signal.

**Consequence for the codebase:** the `PostToolUseFailure(is_interrupt=True)` branch in `agent_activity.py` is **dead on 2.1.170** (kept for older/future versions; `is_interrupt` is undocumented). Do not trust it as the mid-tool interrupt signal.

**The fix (shipped):** `agent_interrupt.py` — `EscapeWatcher` is an app-level Qt event filter that OBSERVES (never consumes) `Key_Escape` and notifies `AppController.on_terminal_escape`. Gated on `should_arm_interrupt_clear` (agent surface visible + a focused agent + an interruptible state — `thinking`/`working`), it arms a ~1.5s grace-window QTimer that clears the sparkle to idle; any real subsequent hook for that slot cancels it (`_cancel_pending_interrupt_clear` in `_on_agent_hook`). The agent-surface gate is load-bearing: the editor (vim Esc) and shell Esc must never touch agent state. Verified live: a real Escape typed (via `wtype`) into a running dev IDE's agent pane produced `interrupt-clear: slot 1 sparkle cleared after Esc (no hook fired)`.

**How to apply:** any future "agent stuck active" report → first suspect a non-hook turn-end (interrupt) that the event stream can't represent, not a bug in the bridge/reporter. Any event-sourced agent state needs a non-hook fallback edge because Claude will never emit one for interrupts. See [applicationshortcut_masks_terminal_keys](../qt-pyside/applicationshortcut_masks_terminal_keys.md) for why the fix observes Esc at the app filter (which runs in `notify()` before the terminal) rather than via a `Shortcut` (which would consume the key and break the real interrupt).
