# Agent ownership inversion — design + implementation context

> **STATUS: PLANNED (not started).** This is the durable handoff for a multi-repo
> refactor decided via discourse on 2026-06-26. It captures both the phased plan
> AND the exploration findings (so implementation needn't re-explore the IDE +
> shell + orchestrator.nvim). Plan mirror: `~/.claude/plans/shiny-nibbling-shore.md`.
> Prerequisite shipped first: the per-project sessionizer (`session_store.py`,
> `Ctrl+Shift+R`/`Ctrl+Shift+S`, reload-in-place) — see that for the feature this
> grew out of.

## Why (the problem)

Capturing a Claude agent's `session_id` for the sessionizer required **editing the
Symmetria shell's `agent-bridge.py`** — because the IDE learns about its *own*
agents by reaching OUT to the shell's bridge and subscribing back. The IDE spawns
the agents, sets their `SYMMETRIA_AGENT_ID`, owns the terminal panes — yet depends
on a third process (and edits to it) for its own children's data. That round-trip
is backwards.

## Target architecture

- The **IDE is the single authoritative owner** of all state about the agents it
  spawns — captured **locally** (session_id + activity), driving its own UI, and
  published **one-way outward** in an IDE-defined, consumer-agnostic schema.
- The shell's `agent-bridge.py` becomes a **dumb, schema-neutral relay** that fans
  IDE records out to the dashboard. Adding a field never touches shell code again.
- **STT is redesigned** off the bridge onto a **direct shell→IDE channel** (the
  IDE's own socket), so the bridge carries *only* IDE→dashboard records.
- Non-IDE (plain-terminal) agents are **dropped** from the dashboard; the global
  Symmetria claude hook is **removed**; **orchestrator.nvim is removed**.

```
TODAY:  agent hooks ─► shell bridge (state machine) ─► dashboard
                                  ▲   │ snapshot (activity+stt)
                          IDE ────┘◄──┘   (round-trip for own data)

TARGET: IDE-owned hook ─► IDE socket ─► IDE state store ─┬─► own UI (sparkles, restore)
        shell STT ──────► IDE socket (direct) ──────────┘   │
                                                            └─► one-way publish ─► shell relay ─► dashboard
```

IDE socket = `$XDG_RUNTIME_DIR/symmetria-ide-agents-<pid>.sock` — the IDE's "agent
server": hook reports in, STT in, (future) third-party queries in.

## Decisions (from discourse)
- Aggregation = **shell as dumb relay** (no new daemon now); protocol IDE-owned + opaque.
- Activity + session_id = **local capture, one-way publish** (kills the round-trip).
- STT = **redesigned to a direct shell→IDE socket** (chosen over keeping it 2-way on the bridge).
- Non-IDE agents **dropped**; global hook **removed**; orchestrator.nvim **removed**.
- IDE socket shaped now to also serve a **future direct-query interface** for third-party dashboards.

---

## Exploration findings (current state — captured 2026-06-26, verify before edit)

### A. IDE ↔ bridge protocol — `src/symmetria_ide/agent_bridge.py`
PUBLISH verbs (GUI→bridge): `hello` (`_replay_registration` ~262), `sync` (~270),
`subscribe` (~271), `added` (`notify_spawn` ~180), `removed` (~186), `focus` (~193),
`updated` (`_flush_titles` ~207). Per-instance fields: `buf`, `active`, `title`,
`session_id`, `harness`, plus (in payload) `cwd`/`project`/`spawn_type`/`color_idx`/
`dangerous`/`spawned_at`/`inject_via`.
SUBSCRIBE: `snapshot_received` Signal (~92), emitted from reader thread (~316);
`inject_requested` Signal (~100, emitted ~314) — a SEPARATE channel from snapshots.

### B. Snapshot consumption (the round-trip to remove) — `app.py`
`_on_bridge_snapshot` (~3169-3211) drives: (1) `_term_agent_activity` mirror for
sparkles (filters `f"{os.getpid()}_"`, fields `{state,tool,agentType}`, ~3200-3207,
emits `agentActivityChanged` only on change), (2) `session_id` backfill into
`_term_agents[slot]` (~3190-3199), (3) `_mirror_stt_state` (~3211-3243).
`agentActivity` Q_PROPERTY (~2262-2284) → `AgentTopBar.qml:181` →
`Symmetria.Agents.UI` AgentChip reads `activity.state` for sparkles.
→ Local-capture replaces (1)+(2) from IDE socket events; (3) moves to direct STT.

### C. Per-agent injection seam (template) — `browser_mcp.py` + `agent_harness.py`
`agent_config_path` writes `{tempdir}/symmetria-browser-mcp-<pid>_<slot>.json`
(prefix `_CONFIG_PREFIX` ~65, write ~466-475), injected by `spawn_argv`
(`agent_harness.py:109-110`) via `harness.mcp_config_flag="--mcp-config"` (claude;
None for opencode). `reap_orphan_configs` (~115-142) parses leading pid, reaps if
`/proc/<pid>` absent. → Mirror this exactly for a `--settings` writer.
**Confirmed**: `claude --settings <file-or-json>` loads ADDITIONAL settings; hooks
shape = the `~/.claude/settings.json` form (`{Event:[{matcher?,hooks:[{type:"command",command,async}]}]}`).
Injected hooks ADD to globals (so pre-Phase-3 both fire, to different destinations).

### D. STT path (current) — bridge-mediated, two-way
Shell `set_stt_state` (agent-bridge.py ~262-283) broadcasts `stt` in every snapshot
(~486). Shell `stt-inject.sh` (~170-229) sends `{type:"inject", target_nvim_pid,
buf, text, submit}` → bridge `handle_inject` (~573) routes to the IDE's publisher
writer, waits for result (~3s). IDE `_on_bridge_inject` (app.py ~3245-3289) →
`agentInjectRequested(slot,text,submit,request_id)` → QML PTY write →
`agent_inject_done` (~3295) → `send_inject_result` (agent_bridge.py ~211). IDE
`_mirror_stt_state` (~3213-3243) drives `sttTargetSlot`/`sttTranscribing` chip dot.
→ Redesign: IDE socket accepts `stt_recording {buf,on}` + `stt_inject {buf,text,submit}`
and replies; shell connects DIRECTLY to `symmetria-ide-agents-<target_pid>.sock`
(pid from the agent id's `<ide_pid>_<slot>`). Remove all STT from the bridge + the
IDE `_on_bridge_inject`/`inject_requested`/`_mirror_stt_state`-from-snapshot;
IDE bridge connection becomes publish-only (drop `subscribe`).

### E. Activity state machine (port into IDE) — shell `agent-bridge.py`
Hook event → state: SessionStart→`starting` (source=clear→`clearing`),
UserPromptSubmit→`thinking`, PreToolUse→`working`, PostToolUse/Failure→`thinking`,
PermissionRequest→`needs_permission`, Stop→`idle`, SubagentStart→`working`,
SubagentStop→`thinking` (if depth>0; dropped at depth 0 = recap),
PreCompact→`thinking`, SessionEnd→`offline`, Notification(idle_prompt)→`idle`.
Subagent depth tracking (~743-761); out-of-order detection via `event_ts_ns`
(~728-734, logs not drops); idle/offline → `_pop_activity` (~767) which preserves
sticky `_agent_types`/`_session_ids`. Hook stdin fields: `hook_event_name`,
`session_id`, `tool_name`, `tool_input.command`, `permission_mode`, `source`.
Hook (`symmetria-agent-hook.py`) sends `{type:"activity", agent_id, agent_type,
state, tool, in_plan_mode, hook_event, event_ts_ns, session_id}` (~291-308) +
`{type:"notification",...}` (~171-198).

### F. Dashboard parity target — shell `services/AgentService.qml`
Per-agent fields the dashboard reads (the minimum the IDE must publish):
`id, nvim_pid, buf, project, title, color_idx, dangerous, active, spawn_type,
spawned_at, terminal_pid, nvim_socket, remote, agent_type, activity_state,
activity_tool, session_id, in_plan_mode, inject_via`. Global: `projects`, `stt`.
Derived: `agentCount`, `mergeActive`, `sortedProjects`, `workspaceMap` (terminal_pid→).

### G. Global hook registration — `~/.claude/settings.json`
`symmetria-agent-hook.py` registered (async:true) on SessionStart, UserPromptSubmit,
PreToolUse, PostToolUse(+Failure), Stop, PermissionRequest, SubagentStart/Stop,
PreCompact, SessionEnd, Notification(matcher `idle_prompt`, argv `idle-notification`).
**KEEP** the unrelated `claude-sudo-askpass-hook.sh` on PreToolUse:Bash when removing.

### H. orchestrator.nvim + IDE dead code
Plugin spec: `~/.dotfiles/.config/nvim/lua/jc/plugins/orchestrator.lua` (dev → `~/projects/orchestrator.nvim`).
Bridge publisher: `~/projects/orchestrator.nvim/lua/orchestrator/bridge.lua` (same verbs as the IDE).
IDE dead code: `AppController.send_editor_keys` (app.py ~1503-1521) — never called
(tests reference it: test_app_controller_central_surface.py ~432-478,
test_main_qml_terminal_wiring.py ~376-379 asserts the relay is absent).

---

## Phased plan (each phase shippable + testable; no dashboard-dark / STT-dark window)

1. **IDE captures locally (additive):** new `agent_events.py` (socket server),
   `runtime/symmetria-ide-agent-hook.py` (reporter), per-agent `--settings` writer +
   `spawn_argv` injection (`SYMMETRIA_IDE_AGENT_SOCK` env), ported state machine
   (`agent_activity.py`), drive `_term_agent_activity`+`session_id` from socket.
2. **IDE publishes computed state; bridge relays it** (prefer IDE-published activity).
3. **Cut over** (couple with 2): remove global hook from `~/.claude/settings.json`,
   strip bridge state machine → dumb relay, **revert the `_session_ids` sticky change**,
   IDE stops deriving its state from snapshots.
4. **Redesign STT** to the direct IDE socket; remove STT from bridge + IDE;
   IDE bridge connection becomes publish-only.
5. **Remove orchestrator.nvim** + `send_editor_keys` + stale comments.

## Risks
- Phases 2+3 coupled (don't leave two activity sources fighting).
- Reporter must be fire-and-forget fast (never stall a claude turn).
- Multi-IDE STT addressing relies on `<ide_pid>` in the agent id (shell has it).
- Reporter path must resolve for repo-run AND installed launchers.
- opencode still gets no hook (no `--settings` parity) — known gap, unchanged.

## Verification
Per IDE phase: `PYTHONPATH=src python -m pytest tests/ -v` + headless smoke
(`QT_QPA_PLATFORM=offscreen SYMMETRIA_IDE_SCREENSHOT=…`). Live: 2 agents → sparkles
(local), dashboard parity (shell), dictation (direct STT), reload-restore (local
session_id), `ps`/`hyprctl` hygiene. Add unit tests for the state-machine port +
the socket parser.
