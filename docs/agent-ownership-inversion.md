# Agent ownership inversion — design + implementation context

> **STATUS (2026-06-26): Phases 1–3 SHIPPED for claude (functional cutover live);
> Phase 4 (STT) + Phase 5 (orchestrator removal) PLANNED.** Multi-repo refactor
> decided via discourse 2026-06-26. Captures the phased plan AND the exploration
> findings (so implementation needn't re-explore the IDE + shell + orchestrator).
> Plan mirror: `~/.claude/plans/shiny-nibbling-shore.md`. Grew out of the
> per-project sessionizer (`session_store.py`, `Ctrl+Shift+R`/`Ctrl+Shift+S`).
>
> **What's live:** the IDE captures its own **claude** agents' activity +
> session_id locally (Phase 1), publishes it to the bridge (Phase 2 IDE-side),
> the bridge prefers those published fields (Phase 2 shell-side), and the global
> `symmetria-agent-hook.py` is removed from `~/.claude/settings.json` (Phase 3
> functional cutover). Commits: IDE `a3aebdc`+`b7210af`+`d1df640`; shell bridge
> `a88138a0`; dotfiles `a329a67`.
>
> **Correction to the original Phase 3 plan — the bridge state machine is NOT
> stripped, deliberately.** opencode agents report via their own plugin (NOT the
> removed claude hook), the IDE injects no reporter for opencode (`settings_flag`
> is None), and the shell-half *falls back* to the bridge's computed activity
> exactly when `inst` carries none — which is the opencode case. So the state
> machine + the IDE's `_on_bridge_snapshot` mirror MUST stay until opencode also
> has local capture; stripping them now would break opencode. The "dumb relay"
> end-state is therefore gated on opencode local-capture (or dropping opencode).
>
> **Pending live verification (user-driven):** reload the shell (so the bridge
> runs prefer-inst), run an IDE with the new code (dev, or promote dev→stable —
> the **stable daily-driver shows no claude activity until promoted**), then
> confirm: a claude agent's sparkles + dashboard activity come from the IDE, and
> opencode still works via the bridge fallback.

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

1. **IDE captures locally (additive) — ✅ SHIPPED 2026-06-26.** New
   `agent_events.py` (socket server at `$XDG_RUNTIME_DIR/symmetria-ide-agents-<pid>.sock`,
   accept thread + per-connection handler threads, `emit_gc_safe`),
   `runtime/symmetria-ide-agent-hook.py` (DUMB reporter — forwards curated raw
   hook fields, no mapping), `agent_activity.py` (the WHOLE machine: event→state
   map + subagent-depth + idle-pop + ooo, ported from BOTH the shell hook and the
   shell bridge — pure/unit-tested), and the AppController wiring (`_on_agent_hook`
   drives `_term_agent_activity`+`session_id`; `_locally_captured_agents` makes
   local AUTHORITATIVE via a seed-from-local rebuild in `_on_bridge_snapshot`;
   `_forget_local_agent` on close).
   - **Simplification vs the original plan:** `claude --settings` accepts an
     INLINE JSON string (verified via `claude --help`), and the reporter learns
     its per-agent identity from `SYMMETRIA_AGENT_ID` in the env — so the settings
     are identical for every agent. We pass ONE static inline `--settings` string
     (built by `app._reporter_settings_json`), NOT a per-agent file. No temp file,
     no orphan-reaping for settings (unlike the browser MCP config, which needs a
     per-agent header). `agent_harness.AgentHarness.settings_flag` (claude
     `--settings`, opencode None) + `spawn_argv(settings_json=, agent_sock_path=)`
     do the injection; `SYMMETRIA_IDE_AGENT_SOCK` rides the env wrapper.
   - **Phase-1 coexistence is intentional:** the injected hooks ADD to claude's
     global settings (which still register the shell hook), so BOTH fire — the IDE
     socket AND the bridge. Phase 3 removes the global hook + the bridge-derived
     path; `_locally_captured_agents` then becomes unconditional.
2. **IDE publishes computed state; bridge prefers it — ✅ SHIPPED 2026-06-26.**
   IDE-side (`a88138a0` is shell; IDE is `d1df640`): `AgentBridgeClient.notify_activity`
   stores activity_state/tool/in_plan_mode/session_id on the instance record (so a
   reconnect `sync` replays them) + sends an `updated` delta; `_on_agent_hook`
   publishes on any activity change / fresh session_id. Shell-side
   (`agent-bridge.py` `a88138a0`): `_snapshot_line` PREFERS the published `inst`
   fields (present-key check, so a published idle `""` wins) and falls back to the
   computed `_activities`/`_session_ids`; the `updated` whitelist widened to carry
   them. Additive — opencode + non-publishers behave as before.
3. **Cut over (functional) — ✅ SHIPPED 2026-06-26 for claude.** Removed the 12
   `symmetria-agent-hook.py` registrations from `~/.claude/settings.json`
   (dotfiles `a329a67`; kept claude-sudo-askpass / hypr-config-check /
   stop-timestamp). claude agents are now IDE-owned end-to-end.
   **NOT done, deliberately (corrects the original plan):** stripping the bridge
   state machine, reverting the `_session_ids` sticky change, and removing the
   IDE's `_on_bridge_snapshot` mirror — all **still required by opencode**, which
   reports via its own plugin (not the removed claude hook) and rides the
   shell-half's computed-activity fallback. The strip is gated on opencode
   local-capture (or dropping opencode), not just on risk. The bridge's
   `reconcile_claude_sessions` / `check_stuck_working` / `reap_stale_activities`
   are now **claude-blind** (they iterate `_activities`, empty for claude
   post-hook-removal) — a cancelled claude agent that fires no hook now relies
   solely on the ~60s idle `Notification` to clear (the fast CLI-reconcile
   backstop no longer reaches IDE agents). Rework them to operate on the
   published `inst` activity, or remove them, in the eventual strip.
   **Live verification still owed** (needs shell reload + a new-code IDE; the
   stable daily-driver is claude-activity-dark until promoted).
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
