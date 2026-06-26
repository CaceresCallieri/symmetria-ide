---
name: agent-ownership-inversion
description: Multi-repo refactor making the IDE the single source of truth for its agents; Phases 1-3 + Phase 4 IDE-side SHIPPED for claude, Phase 4 shell-side + Phase 5 planned
metadata: 
  node_type: memory
  type: project
  originSessionId: dab4cce2-971c-430b-88d7-7b19226fd6bd
---

Phases 1-3 SHIPPED for claude (functional cutover live, 2026-06-26); Phase 4 (STT) +
Phase 5 (orchestrator removal) PLANNED. Make the Symmetria IDE the **single
authoritative owner** of all state about the agents it spawns: capture session_id +
activity **locally** (an IDE-injected `claude --settings` reporter hook → an IDE-owned
socket `$XDG_RUNTIME_DIR/symmetria-ide-agents-<pid>.sock`), drive its own UI from that,
publish **one-way** to the bridge. **STT** to be redesigned onto a direct shell→IDE
socket (Phase 4). orchestrator.nvim to be removed (Phase 5).

**Shipped:** IDE `a3aebdc`+`b7210af` (P1 local capture: agent_events.py socket server,
agent_activity.py pure state machine, runtime/symmetria-ide-agent-hook.py dumb reporter,
agent_harness.settings_flag + spawn_argv inline-`--settings` injection, `_on_agent_hook`
+ `_locally_captured_agents` seed-from-local) + `d1df640` (P2 IDE-side
`AgentBridgeClient.notify_activity` publish). Shell `a88138a0` (P2 shell-side:
`_snapshot_line` PREFERS published `inst` activity, falls back to computed; `updated`
whitelist widened). Dotfiles `a329a67` (P3: removed 12 symmetria-agent-hook.py regs from
~/.claude/settings.json; kept claude-sudo-askpass etc). KEY: `--settings` takes INLINE
JSON → one static string for all agents, no per-agent file.

**Correction to original plan — state machine NOT stripped (deliberate):** opencode
reports via its own plugin (not the removed claude hook) + IDE injects no reporter for it,
so it rides the shell-half's computed-activity FALLBACK. The bridge state machine + IDE
`_on_bridge_snapshot` mirror MUST stay until opencode local-capture lands; stripping now
breaks opencode. So the `_session_ids` sticky revert + dumb-relay strip are gated on
opencode, NOT yet done.

**Live verification owed:** reload shell (bridge runs prefer-inst) + run new-code IDE
(dev, or promote dev→stable — STABLE daily-driver is claude-activity-dark until promoted);
confirm claude sparkles+dashboard come from IDE, opencode still works via fallback.

**Phase 4 (STT direct channel) — IDE-side SHIPPED additive (`94afc14`):** decision
PURE-DIRECT (drop remote/SSH STT, remove bridge inject path entirely). agent_events now
dispatches by type: stt_recording {buf,transcribing} fire-and-forget → _on_stt_recording
(chip dot); stt_inject {buf,text,submit} REQUEST/REPLY — server stamps request_id, blocks
on a Future resolved by resolve_inject←agent_inject_done, writes stt_inject_result back.
_dispatch_inject shared w/ bridge path; agent_inject_done routes direct-first/bridge-fallback
(both work during transition). STT buf: slot / -1 focused / 0 clear. **PENDING shell-side:**
stt-inject.sh + AgentService._pushSttState/SttJob → connect direct to
$XDG_RUNTIME_DIR/symmetria-ide-agents-<ide_pid>.sock (shell has ide pid=nvim_pid at inject);
remove bridge handle_inject/handle_inject_result + stt snapshot field + set_stt_state. Shell
agentbar dot reads LOCAL AgentService (unaffected). **PENDING IDE cleanup (after shell live):**
remove _on_bridge_inject + inject_requested + _mirror_stt_state; KEEP subscribe (opencode).

Full design + the exploration findings (bridge protocol, activity state-machine
rules, dashboard parity fields, STT flow, injection seam, dead-code inventory, all
with file:line) live in **`docs/agent-ownership-inversion.md`**. Phased plan mirror:
`~/.claude/plans/shiny-nibbling-shore.md`. 5 phases, each independently shippable.

**Why:** the just-shipped sessionizer ([[startup_optimization_followups]] sibling
era) forced an edit to the shell's `agent-bridge.py` just to capture an IDE-private
`session_id` — the IDE reaching OUT to a third process for its own children's data.
The inversion makes the IDE own its agents and the shell merely visualize.

**How to apply:** start at Phase 1 in the docs (additive IDE-local capture — nothing
breaks). When the inversion ships, the transitional shell `_session_ids` sticky
change gets reverted (Phase 3) and CLAUDE.md's "terminal-agent runtime" + bridge
sections need updating to the new topology.
