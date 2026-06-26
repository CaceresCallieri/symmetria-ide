---
name: agent-ownership-inversion
description: Multi-repo refactor making the IDE the single source of truth for its agents; Phase 1 SHIPPED (additive), Phases 2-5 planned
metadata: 
  node_type: memory
  type: project
  originSessionId: dab4cce2-971c-430b-88d7-7b19226fd6bd
---

Phase 1 SHIPPED (additive, 2026-06-26); Phases 2-5 PLANNED (decided via discourse
2026-06-26). Make the Symmetria IDE the **single authoritative owner** of all state
about the agents it spawns: capture session_id + activity **locally** (an IDE-injected
`claude --settings` hook → an IDE-owned socket
`$XDG_RUNTIME_DIR/symmetria-ide-agents-<pid>.sock`), drive its own UI from that, and
publish a **one-way, IDE-defined feed** outward. The shell's `agent-bridge.py` becomes
a **dumb schema-neutral relay** for the dashboard. **STT is redesigned** onto a direct
shell→IDE socket (off the bridge). Non-IDE agents dropped; global Symmetria claude hook
removed; **orchestrator.nvim removed**.

**Phase 1 (live, additive):** `src/symmetria_ide/agent_events.py` (socket server),
`agent_activity.py` (the whole state machine, pure), `runtime/symmetria-ide-agent-hook.py`
(dumb reporter), `agent_harness.settings_flag`/`spawn_argv` injection, AppController
`_on_agent_hook` + `_locally_captured_agents` (local AUTHORITATIVE; bridge preserved via
seed-from-local rebuild). KEY: `--settings` takes INLINE JSON → one static string for
all agents, no per-agent file. NOT committed yet as of this writing. Live claude E2E
still pending (needs composited session). Bridge path still active (removed in Phase 3).

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
