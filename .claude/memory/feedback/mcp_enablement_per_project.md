---
name: mcp-enablement-per-project
description: "Resource-costly agent MCP/capabilities gate PER PROJECT (default off, IDE-owned), never per agent."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: c1882ebc-4b92-4b54-9e54-6d592472bb6f
---

When an agent capability carries a per-instance resource cost — e.g.
chrome-devtools-mcp spawns an ~80–150 MB `npx` Node process **per agent**
(stdio-only, no shared-server mode) — enable it **per PROJECT, default OFF,
opt-in once**, via a **harness-agnostic, IDE-owned gate** placed BEFORE any
harness-specific injection. NOT per agent.

The user explicitly rejected a per-agent spawn-menu toggle and chose a
per-project gate: a **committable `<repo>/.symmetria/ide.json` marker**
(`{"browser_agents": true}`), default off, flipped from a keyboard-first
**MCP-toggles popup** (`Ctrl+Shift+M` → `w` for chrome) — NOT a persistent
top-bar affordance (the user wants the AgentTopBar kept clean), and the
popup is meant to host more per-project MCP toggles over time.

**Why:** a project either does web work (→ browser) or doesn't (this IDE
itself: native Qt/QML); per-agent is too granular and the user opens many
agents across many windows, so the RAM compounds. The IDE is already
one-instance-per-project (see [[multi_instance_topology]]), so the project
is the natural boundary, and the IDE is the canonical control surface (see
[[ide_owns_keybind_layer]]). IDE-side gating (omit the injection) beats each
harness's own `/mcp` disable because (a) it's harness-agnostic — one gate
covers claude + opencode + future agents — and (b) it actually prevents the
process from spawning, rather than disabling a server the agent already
launched. The per-agent Node process is the RAM cost; gating injection is
what saves it (the embedded engine's CDP debug port stays cheaply always-on).

**How to apply:** when adding/injecting any MCP server into spawned agents,
gate it per-project (default off) at the single point before the
harness-specific flag — don't propose per-agent-spawn toggles for
resource-costly MCP. Shipped example: `project_browser_marker.py` +
`AppController._project_browser_enabled` → `browser_mcp.agent_config_path(...,
browser_enabled=...)`; see CLAUDE.md "The browser panes" Stage 5.
