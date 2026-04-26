# Agent dashboard integration

How Symmetria IDE will participate in the existing Symmetria Shell agent dashboard, and the planned deprecation of `orchestrator.nvim`.

This document is the canonical reference for a long-horizon architectural commitment that crosses three repositories:

- **Symmetria IDE** (this repo) — the new home for Claude Code workflows.
- **Symmetria Shell** (`~/.config/quickshell/symmetria`) — owns the agent dashboard UI (`modules/agentbar/`) and the aggregator (`scripts/agent-bridge.py`).
- **`orchestrator.nvim`** (`~/projects/orchestrator.nvim`) — the existing NeoVim plugin currently driving Claude Code from a terminal.

It is intentionally written from the perspective of a future agent who has no prior context. Read end to end before changing how the IDE talks to the bridge.

## Today's dashboard pipeline

The agent dashboard's activity inference does **not** come from terminal-output scraping. It comes from **Claude Code's own hook system**, configured globally in `~/.claude/settings.json`.

```
Claude Code CLI                      Symmetria Shell
─────────────────                    ───────────────────────
PreToolUse / PostToolUse / Notification / Stop / SubagentStart / SubagentStop / SessionStart / UserPromptSubmit
        │
        ▼
~/.config/quickshell/symmetria/scripts/symmetria-agent-hook.py
        │  reads SYMMETRIA_AGENT_ID from env (set on spawn)
        │  writes one JSON line per event
        ▼
/run/user/$UID/symmetria-agents.sock           (Unix socket server)
        │
        ▼
agent-bridge.py
        │  aggregates per agent_id, derives activity_state ∈ {idle, working},
        │  tracks activity_tool, in_plan_mode, dedupes recap subagents,
        │  runs stuck-state watchdog (>120s in "working" → log warning)
        │  writes consolidated JSON lines to stdout
        ▼
Symmetria Shell — services/AgentService.qml (Process + SplitParser)
        │  exposes .agents, .projects, .sortedProjects, .agentCount
        │  groups by Hyprland workspace, paints AgentChip / ProjectGroup
        ▼
modules/agentbar/  — bubbles in the bar
```

The orchestrator.nvim role in this pipeline is narrower than it looks: `bridge.lua` exists only to **register** with `agent-bridge.py` on agent spawn (announcing `nvim_pid`, `terminal_pid`, `project`, `agent_id`). All activity state is downstream of Claude Code's hooks, which fire **regardless of how Claude Code was invoked** — orchestrator-spawned, plain `claude` in any terminal, `claude resume` from a script, SSH-tunneled remote.

## Why the SDK alone would be a regression for this dashboard

Symmetria IDE owns its Claude session via the `@anthropic-ai/claude-agent-sdk` Node sidecar (see `CLAUDE.md` § "The agent backend"). Tempting reading: "we have a structured event stream now, drop the hooks." That would silently break the dashboard for **every Claude session not running inside the IDE**.

Coverage matrix:

| Claude session source                              | Hook visible? | SDK visible? |
|----------------------------------------------------|---------------|--------------|
| `orchestrator.nvim` terminal spawn                 | yes           | no           |
| Plain `claude` in a Ghostty tab                    | yes           | no           |
| `claude resume` from any script                    | yes           | no           |
| SSH-tunneled `claude` on a remote box              | yes           | no           |
| Symmetria IDE sidecar (this repo)                  | yes           | yes          |

Hooks fire for any Claude Code installation that loads `~/.claude/settings.json` — they're host-agnostic. The SDK only sees sessions whose subprocess **we** spawned. Killing the hook path to "consolidate on the SDK" would shrink the dashboard from "all Claudes on the system" to "Claudes spawned by the IDE" — an obvious net loss before the IDE replaces every Claude entry point on the machine, which is years out at best.

## Where the SDK is strictly better

The hook-based feed is *coarse*. The SDK feed is *rich*. The IDE-owned sidecar already sees information the hooks can't carry:

| Dimension                | Hook signal                          | SDK signal                                                           |
|--------------------------|--------------------------------------|----------------------------------------------------------------------|
| Per-event latency        | ~50–100ms (Python subprocess spawn)  | ~0ms (in-process JS emit)                                            |
| Tool detail              | tool name + event type only          | full tool inputs (file paths, bash command text, edit diffs)         |
| Permission decisions     | "happened" — no intercept            | `canUseTool` callback + structured `permission_request` envelope     |
| Permission mode changes  | inferable from `Notification` only   | live `permission_mode_changed` (already shipped in IDE Phase 2)      |
| Token usage / cost       | not available                        | `result.total_cost_usd` per turn                                     |
| Rate limits              | not available                        | `rate_limit_event` + `usage` envelopes                               |
| Streaming partials       | not available                        | `stream_event.text_delta` (live token-by-token assistant text)       |

The conclusion is therefore not "hooks vs SDK" but "hooks AND SDK as **complementary feeds into the same bridge**." Both write to `agent-bridge.py`'s socket; the bridge tags each agent with the source of its richest feed; the dashboard renders extras conditionally.

## Target architecture: dual-feed dashboard

```
Plain `claude` / orchestrator-spawned terminal      Symmetria IDE sidecar
       │                                                    │
       │  hook-shaped messages                              │  SDK→bridge emitter
       │  (PreToolUse, PostToolUse, …)                      │  (translates SDK events
       │                                                    │   into hook-shaped lines
       │                                                    │   with optional richness)
       └────────────┬───────────────────────────────────────┘
                    ▼
         /run/user/$UID/symmetria-agents.sock
                    │
                    ▼
              agent-bridge.py     (per-agent record now carries optional
                    │              cost_usd, current_tool_input, permission_mode
                    ▼              when source == "sdk")
              AgentService.qml
                    │
                    ▼
            AgentChip.qml         (renders extras conditionally on source)
```

Rules of the dual feed:

1. **Wire protocol stays the same.** The bridge does not learn a new dialect for SDK-sourced agents. The SDK→bridge emitter inside the IDE sidecar translates SDK events into the existing `{agent_id, hook_event, state, tool, in_plan_mode, …}` JSON-line shape. New SDK-only fields ride alongside as **optional** keys (`cost_usd`, `current_tool_input`, `permission_mode`, `source: "sdk" | "hook"`).
2. **Bridge schema is additive only.** Existing consumers keep working when the new fields are absent. The bridge does not require the SDK source to be present; it does not require the hook source to be present; it accepts whichever (or both) write to its socket per agent.
3. **Dashboard branches on `source`.** When `source == "sdk"`, the chip can show richer info (current tool input, cost so far, permission-mode pill). When `source == "hook"`, the chip falls back to today's bubble. There is no "downgrade path" — agents stay on their richest available source for their lifetime.
4. **Hook coverage stays as the universal floor.** Even after every Symmetria IDE session feeds the SDK source to the bridge, the hook pipeline remains for any Claude session outside the IDE. We never disable the global hook configuration as part of any IDE feature work.
5. **Registration is per-source.** SDK-sourced agents register from the sidecar (with `nvim_pid: 0`, `terminal_pid: <our IDE process pid>`, `project: <cwd>`, `agent_id: <sdk session id or our generated id>`). The bridge already keys per `agent_id`, so the existing aggregation logic carries over unchanged.

## Implementation surface (when this work begins)

This is the rough shape of the integration commit when we get to it — written so a future agent can pick it up cold:

**New file:** `sidecar/src/emitters/agent-bridge.ts`
- Opens a stream connection to `/run/user/$UID/symmetria-agents.sock` (env override: `SYMMETRIA_AGENT_SOCKET`).
- On SDK `system: init`, emits `{type:"register", source:"sdk", agent_id:<session id>, terminal_pid:<process.pid>, project:<process.cwd()>, ...}`.
- For each subsequent SDK message, translates to a hook-shaped envelope:
  - `PreToolUse` ← SDK `assistant` message containing `tool_use` block (one envelope per tool_use).
  - `PostToolUse` ← SDK `user` message containing `tool_result` block (already filtered by `_extract_tool_result_blocks` in `session_host.py`).
  - `Notification` ← SDK `permission_request`.
  - `Stop` ← SDK `result`.
- Carries optional richness fields: `tool_input` (from the SDK `tool_use.input`), `cost_usd` (from `result.total_cost_usd`), `permission_mode` (from the sidecar's local `currentMode` variable — the authoritative source per `CLAUDE.md` gotcha #25).
- Failure-quiet: if the socket isn't there (Symmetria Shell not running), the emitter logs once and no-ops. The IDE must not depend on the dashboard being present.

**Modifications:**
- `sidecar/src/index.ts`: instantiate the emitter at startup; pass each translated event in parallel with the existing stdout JSONL stream that the IDE itself consumes. The IDE event stream and the bridge event stream are siblings, not in series.
- `agent-bridge.py`: extend the per-agent record to accept optional `cost_usd`, `current_tool_input`, `source` keys. Leave existing aggregation and watchdog logic untouched.
- `services/AgentService.qml`: surface the new fields as `agent.source`, `agent.costUsd`, `agent.currentToolInput`, `agent.permissionMode` (read-only, undefined when absent).
- `modules/agentbar/AgentChip.qml`: conditionally render the richer chip variant when `agent.source === "sdk"`.

**Out of scope for the integration commit:**
- Replacing AgentChip's bubble layout with a vertical agent strip — the user has flagged this as a possible future redesign; the integration commit must keep the existing layout to avoid coupling two changes.
- Two-way control surface (the dashboard sending commands to the IDE — focus pane, switch session, set mode). This is a separate, larger initiative; the existing `IpcHandler` block in `AgentService.qml` is the precedent for how it would land.
- Removing the hook pipeline. Never do this as part of integration work. Hook coverage is non-negotiable for non-IDE Claude sessions.

## Multi-instance is required

The user has confirmed that **multi-instance management is fundamental to the IDE's vision** — the same workflow as `orchestrator.nvim`'s `<leader>an / aN / ar / aR / ac / aC` keybinds and `<C-1>..<C-5>` focus-by-index will exist in the IDE. This means the IDE must spawn N parallel sidecars (each its own `SessionHost`), each emitting to the bridge with a distinct `agent_id`. The bridge already handles N-to-1 aggregation per project; the lift on the bridge side is zero.

The shape of this on the IDE side is a `SessionHostPool` sibling to the current single `SessionHost`. Open design questions for when this work begins:

- **Per-instance UI**: parallel agent panes (tab strip in the agent column) vs a single pane with an instance switcher? Defer until the multi-instance keybinds are wired so we can iterate against real cadence (consistent with `.claude/memory/feedback/ui_surface_discipline.md`).
- **Spawn semantics**: `fresh` / `continue` / `resume` map cleanly onto SDK options (`continue`, `resume <session-id>`); the spawn menu UI from `orchestrator.nvim/lua/orchestrator/spawn_menu.lua` is the reference for the keybind shape (single-key shortcuts `n/c/r` and `N/C/R`).
- **Permission dialog routing**: with N panes, a `permission_request` belongs to a specific pane — the existing `request_id` already disambiguates; the routing is a UI question, not a protocol question.

## Deprecation path for `orchestrator.nvim`

The plugin is **not** retired the moment the IDE has multi-instance. It is retired when the IDE meets these gates:

1. ✅ **SDK→bridge emitter shipped** — IDE-spawned sessions appear in the dashboard with the same activity inference as today's terminal sessions.
2. ⏳ **Multi-instance shipped** — `<leader>an / aN / ar / aR / ac / aC` work in the IDE with the same semantics as the plugin (per-project filtering, fresh / continue / resume, color-indexed instances).
3. ⏳ **Spawn menu shipped** — single-key spawn flow lands as IDE chrome (don't port the floating window verbatim — adapt it to the IDE's design language; multi-tab markdown prompt editor is **explicitly scrapped**, per user direction).
4. ⏳ **Edit tracker (deferred but not abandoned)** — when the user wants better edit navigation than orchestrator's quickfix, we build it on top of the IDE's structured `tool_result` rows (cleaner than orchestrator's terminal-scraping). This gate is not blocking — the plugin can be retired before this lands as long as the user does not currently rely on `<leader>ae / aj`.

When all required gates clear:

- Tag `orchestrator.nvim` at its last working commit.
- Update its README to point at Symmetria IDE as the successor.
- Remove the plugin entry from `~/.dotfiles/.config/nvim/lua/jc/plugins/orchestrator.lua`.
- Remove the cross-reference from this repo's `CLAUDE.md` § "Related projects".
- Leave the bridge and hook script alone — they remain the universal floor for any non-IDE Claude session and for backward compatibility with anyone else who picks up `orchestrator.nvim` from the archive.

The plugin's source stays accessible (archive tag, not deletion). If it ever needs to come back — for a remote-only nvim workflow without the IDE running, for example — the archive is the way back in.

## Anti-patterns to avoid

These are the predictable wrong turns:

- **Replacing the hook pipeline with the SDK feed.** Discussed above — silently shrinks dashboard coverage to IDE-only sessions.
- **Inventing a new wire protocol for SDK-sourced agents.** The bridge already has a working aggregation; teaching it a second dialect doubles its complexity and forks the dashboard's chip rendering. Keep the SDK→bridge emitter as a translator, not as a new schema.
- **Coupling the dashboard refresh to IDE startup.** The dashboard must keep working when the IDE is closed. The IDE depends on the dashboard at all only insofar as the SDK→bridge emitter is fail-quiet — no IDE feature should fail because the bridge socket is gone.
- **Letting the IDE rename or rebind the plugin's keybind set.** The user has explicitly committed to the same keybinds (`<leader>an / aN / ar / aR / ac / aC`, `<C-1>..<C-5>`, `<C-S-q>`) for muscle-memory continuity. New keybinds are additive; existing ones are reproduced verbatim.
- **Naming the IDE "Orchestrator IDE."** The agent pane is one feature in a larger product. The orchestration role lives inside the IDE; the IDE is not only that role.

## Cross-references

- `CLAUDE.md` § "The agent backend (Node SDK sidecar)" — current Phase 2 implementation; the SDK side of this future work.
- `CLAUDE.md` gotcha #25 — why the sidecar's local `currentMode` is the authoritative permission-mode source (relevant when emitting `permission_mode` to the bridge).
- `~/.config/quickshell/symmetria/CLAUDE.md` § agent dashboard architecture — the Shell side of this dual feed.
- `~/.config/quickshell/symmetria/services/AgentService.qml` — the consumer the SDK→bridge emitter must be wire-compatible with.
- `~/.config/quickshell/symmetria/scripts/agent-bridge.py` — the aggregator the SDK→bridge emitter writes to.
- `~/.config/quickshell/symmetria/scripts/symmetria-agent-hook.py` — the existing per-event emitter that the SDK feed runs alongside, never replaces.
- `~/projects/orchestrator.nvim/lua/orchestrator/bridge.lua` — the existing nvim-side registrar; reference for the registration message shape the SDK→bridge emitter must produce.
- `~/projects/orchestrator.nvim/lua/orchestrator/spawn_menu.lua` — reference for the spawn menu UI semantics the IDE must reproduce.
- `docs/future.md` — long-horizon direction (own WM, gpui rewrite); this document is a near-term complement.
- `docs/phases.md` — phase sequencing; the SDK→bridge emitter and multi-instance pool are Phase 2 follow-ups (not in the placeholder spike scope, but inside Phase 2's broader umbrella).
