---
name: agent-ownership-inversion
description: Multi-repo refactor making the IDE the single source of truth for its agents; Phases 1-4 SHIPPED for claude (STT now pure-direct both sides); Phase 5 = IDE decoupled from orchestrator.nvim (which is KEPT for standalone nvim)
metadata: 
  node_type: memory
  type: project
  originSessionId: dab4cce2-971c-430b-88d7-7b19226fd6bd
---

Phases 1-4 SHIPPED for claude (2026-06-26); Phase 5 IDE-side SHIPPED (orchestrator.nvim
KEPT — see below). Make the Symmetria IDE the **single authoritative owner** of all
state about the agents it spawns: capture session_id + activity **locally** (an
IDE-injected `claude --settings` reporter hook → an IDE-owned socket
`$XDG_RUNTIME_DIR/symmetria-ide-agents-<pid>.sock`), drive its own UI from that,
publish **one-way** to the bridge. **STT** is now a DIRECT shell→IDE round-trip on
that socket (Phase 4, both sides shipped). **Phase 5:** the IDE was DECOUPLED from
orchestrator.nvim (`send_editor_keys` removed); orchestrator.nvim itself is KEPT as
the standalone-nvim runtime (user decision 2026-06-26 — it's deeply wired into the
nvim config: auto-session, snacks dashboard, neo-tree, `<leader>a*`).

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

**Phase 4 (STT direct channel) — FULLY SHIPPED (PURE-DIRECT, both sides; remote/SSH
STT dropped).** IDE-side (`94afc14`): agent_events dispatches by type — stt_recording
{buf,transcribing} fire-and-forget → _on_stt_recording (chip dot); stt_inject
{buf,text,submit} REQUEST/REPLY — server stamps request_id, blocks on a Future resolved
by resolve_inject←agent_inject_done, writes stt_inject_result back. STT buf: slot / -1
focused / 0 clear. **Shell-side SHIPPED:** stt-inject.sh `try_direct_inject` +
AgentService `_pushSttRecording`→`scripts/stt-recording.py` + SttJob env
`STT_IDE_PID`/`STT_IDE_BUF`,`_targetIdePid` connect direct to
$XDG_RUNTIME_DIR/symmetria-ide-agents-<ide_pid>.sock (ide pid=agent.nvim_pid). The bridge's
handle_inject/handle_inject_result/set_stt_state/_stt_state + the `stt` snapshot field + the
parent-control stdin reader were REMOVED (bridge = pure IDE→dashboard relay now; STT never
touches it). `inject_via==="bridge"` KEPT as the IDE-pane discriminator. **IDE cleanup
SHIPPED:** removed _on_bridge_inject + bridge inject_requested signal + send_inject_result +
_mirror_stt_state; agent_inject_done resolves the direct Future only (no bridge fallback);
KEPT subscribe + _on_bridge_snapshot (opencode). Snapshot-STT tests migrated to direct-channel
tests. **Validated:** 1004 IDE tests pass, ruff clean, headless boot smoke green, shell
bridge runtime smoke green. **Live dictation verification still owed (user).**

Full design + the exploration findings (bridge protocol, activity state-machine
rules, dashboard parity fields, STT flow, injection seam, dead-code inventory, all
with file:line) live in **`docs/agent-ownership-inversion.md`**. Phased plan mirror:
`~/.claude/plans/shiny-nibbling-shore.md`. 5 phases (Phase 5 = IDE-decoupled, not
orchestrator-removal — see frontmatter).

**Why:** the just-shipped sessionizer ([[startup_optimization_followups]] sibling
era) forced an edit to the shell's `agent-bridge.py` just to capture an IDE-private
`session_id` — the IDE reaching OUT to a third process for its own children's data.
The inversion makes the IDE own its agents and the shell merely visualize.

**How to apply:** start at Phase 1 in the docs (additive IDE-local capture — nothing
breaks). When the inversion ships, the transitional shell `_session_ids` sticky
change gets reverted (Phase 3) and CLAUDE.md's "terminal-agent runtime" + bridge
sections need updating to the new topology.
