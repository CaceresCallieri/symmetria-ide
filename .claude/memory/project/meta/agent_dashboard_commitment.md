---
name: Agent dashboard dual-feed + orchestrator.nvim deprecation
description: Cross-project commitment — IDE will dual-feed (hooks + SDK) the existing Symmetria Shell agent dashboard; orchestrator.nvim retires once IDE meets gates
type: project
originSessionId: a094e8d9-4dcb-4507-bc47-56d8a4453394
---
The IDE will participate in the existing Symmetria Shell agent dashboard via a **dual-feed** architecture, NOT by replacing the hook pipeline:

- **Hook pipeline stays as the universal floor** — `~/.config/quickshell/symmetria/scripts/symmetria-agent-hook.py` fires for every Claude Code session anywhere on the system; the dashboard's coverage of non-IDE Claudes depends on it. Never disable.
- **SDK→bridge emitter is added to the IDE sidecar** — translates SDK events into the existing wire shape `agent-bridge.py` already accepts, with optional richness fields (`cost_usd`, `current_tool_input`, `permission_mode`, `source: "sdk"`).
- **Multi-instance is fundamental** — IDE will spawn N parallel `SessionHost`s with the same keybinds as `orchestrator.nvim` (`<leader>an / aN / ar / aR / ac / aC`, `<C-1>..<C-5>`, `<C-S-q>`). Same workflow, ported into the IDE.
- **`orchestrator.nvim` retires when the IDE clears these gates**: SDK→bridge emitter shipped, multi-instance shipped, spawn menu shipped. Edit tracker is deferred but not blocking.
- **Scrapped from the orchestrator port**: the floating multi-tab markdown prompt editor (user does not use it).

**Why:** The user committed to this direction in the 2026-04-26 session after auditing `orchestrator.nvim` against the IDE's current surface. The dual-feed design is non-obvious — a future agent's first instinct will be to "consolidate on the SDK and drop the hooks," which silently breaks the dashboard for every plain `claude`, `claude resume`, SSH-tunneled, or third-party-spawned session. The hook pipeline is host-agnostic; the SDK is not.

**How to apply:** Read `docs/agent-dashboard-integration.md` end-to-end before touching the SDK→bridge emitter, multi-instance pool, or `orchestrator.nvim` deprecation. The doc is the canonical reference and includes the implementation surface, anti-patterns, and gate list. Cross-references to the Symmetria Shell side (`AgentService.qml`, `agent-bridge.py`, `symmetria-agent-hook.py`) are listed there. Do not duplicate that doc into memory — it lives at `docs/agent-dashboard-integration.md` and `CLAUDE.md` § "Where to look first" links to it.
