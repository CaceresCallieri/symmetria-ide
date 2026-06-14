---
name: stt-chip-hub-broadcast
description: "IDE STT chip reads the bridge-hub broadcast; the shell dashboard reads local AgentService — a stuck IDE STT animation is a hub bug, not an IDE bug"
metadata: 
  node_type: memory
  type: reference
  originSessionId: c2520dc6-0151-4951-b845-22f8c8f29d04
---

The IDE's AgentTopBar STT soundwave and the Symmetria Shell agentbar's STT
soundwave look identical but are fed by **different sources** — so they can
disagree, and that disagreement is by design:

- **Shell dashboard** computes its target *locally* via
  `AgentService.isAgentSttTarget()` reading `_sttTargetTerminalPid`/`_sttTargetBufId`
  (set/cleared by `SttJob` → `setSttTarget`/`clearSttTarget`).
- **IDE** has no `AgentService`. Its ONLY source is the bridge hub's snapshot
  `stt` field (`{terminal_pid, buf, transcribing}`), which the shell mirrors
  separately via `AgentService._pushSttState()` purely for non-shell consumers.
  The IDE reflects it in `_mirror_stt_state` (app.py) → `sttTargetSlot` /
  `sttTranscribing` QML props.

**Pitfall:** if a stuck STT animation appears in the IDE but NOT the shell
dashboard, do not debug the IDE — `_mirror_stt_state` is a faithful stateless
mirror that clears the instant the hub clears. The root cause is the hub's
`_stt_state` latching: the shell's `clearSttTarget` push fails to stick on
IDE-targeted dictations, and the hub had no self-heal (agent *activity* gets a
staleness watchdog + CLI reconcile; STT had none). Diagnose by probing the live
hub socket (`/run/user/$UID/symmetria-agents.sock`, send `{"type":"subscribe"}`,
read the `stt` field) — a stale field with no active dictation confirms it.

**Fixed (2026-06-14)** in the shell repo `~/.config/quickshell/symmetria/scripts/agent-bridge.py`:
`_stt_state` now self-heals like activity does — cleared when the target agent
enters `"working"` (gated on `"working"` only, so a live recording keeps its
wave), when the target agent is reaped, and via a 5-min `STT_STALENESS_TIMEOUT`
backstop. The fix is HUB-side (shell repo, edited live — no IDE dev→stable
promotion); takes effect on the next shell/bridge restart, which also clears any
currently-latched broadcast. See also [[multi-instance-topology]] (many live IDE
pids share one hub).
