# Future

Things that are years out but influence today's decisions.

## Topology inversion: agent-primary, editor-on-demand

**Timeline:** progressive, over Phases 2.5 through 5+. No single "flip the switch" moment.

**The change:** today's IDE is editor-centric (NvimView is the central pane; agent + file tree are satellites). The long-term shape inverts to *agent-and-navigation-centric*:

- **Terminal pane** becomes the persistent home surface — the IDE's "lobby". You launch into it and use it to navigate (`cd`, `fzf`, autojump, etc.) until you anchor.
- **File tree + git status** stay always-on as the observation stack — but pre-anchor they follow the terminal's cwd, and post-anchor they pin to the anchored root.
- **Agent pane** evolves from "summoned over the editor via `<leader>aN`" (today) into the *default-visible primary surface*. The action you take in the IDE is overwhelmingly "tell the agent what to do", not "type characters into a buffer".
- **NeoVim** shrinks from "the central pane" to "a viewer/editor summoned on demand for specific buffers". You open it when you want to *read* something the agent produced or *hand-edit* a file. It is not the default focus.

**Why this is the direction:** the workflow already trending this way in practice — the user spends increasingly more time directing Claude Code and reviewing what it produces, and less time hand-editing. Today's editor-centric layout is a vestige of how IDEs were used in 2020–2023; an honest 2026+ IDE positions the agent surface where the editor surface used to be.

**Pragmatic compromise for the foreseeable future:** NeoVim still launches in the background pre-anchor and remains the primary edit/view surface post-anchor. The inversion happens *gradually* as the agent pane absorbs more responsibilities (turn grouping, tool-result diffing, inline approve/deny, eventually multi-instance orchestration). When the agent pane covers 80%+ of daily work, the layout flip becomes natural — until then, NeoVim stays central by default.

**Implications for current planning:**
- Phase 2.5 (terminal pane) is the first concrete step toward this topology — the terminal isn't an "extra feature", it's the foundation of the inverted layout.
- The agent pane's current "full-window swap over the editor" pattern (`agentVisible` toggle) will eventually feel backwards. When inversion happens, the agent pane becomes the persistent surface and the editor becomes the visibility-toggled one. Plan for that refactor; don't entrench the current shape.
- The pre-anchor / post-anchor distinction (introduced by the anchor state machine in Phase 2.5) is the architectural seam where this inversion will eventually land. Pre-anchor: terminal-primary, no editor focus. Post-anchor: editor accessible but not default. Long-term post-anchor: agent-primary, editor summoned.

**What this is NOT:** a plan to remove NeoVim. NeoVim remains the editor core forever (or until the gpui rewrite, per the "Own editor core" section below). The change is in *prominence and default visibility*, not in *which tool does the editing*. When the user needs to hand-edit, NeoVim is the tool. The question is only how often that need arises and how it's surfaced.

## Own window manager

**Estimated timeline:** ~2 years.

**Plan:** fork Hyprland, evolve a custom Wayland compositor tuned to Symmetria. AI-assisted code work may make this feasible by 2028.

**Why it matters now:** owning the WM layer enables native inter-application protocols. At that point the IDE's monolithic shape becomes re-examinable — some current internal panes (File Manager, browser) could become standalone apps again, talking to the IDE via the compositor.

This is why today's monolith decision is **current, not permanent**.

## gpui migration

**Target:** rewrite the IDE in Rust on top of `gpui` (Zed's engine) once gpui has a stable public API.

**Why wait:** gpui is pre-1.0 in 2026 with frequent breaking changes. Rewriting now would mean chasing a moving target.

**What the wait buys:** Phases 0–4 in PySide6 teach us exactly what widgets, layouts, and event flows we want. The gpui rewrite begins from a working reference, not a blank page.

## Additional Symmetria apps

Possible, all **standalone** (not absorbed into the IDE):

- Symmetria WhatsApp — in progress, about to replace the main WhatsApp instance.
- Symmetria Discord frontend — possible.
- Symmetria mail frontend — possible.

These share the aesthetic and the Symmetria identity but do not share a process with the IDE.

## Own editor core

Far future. Replace NeoVim's editing buffer itself, motivated only by:

1. gpui migration producing superior text-editing primitives, *and*
2. A specific NeoVim limitation biting hard enough to justify reimplementation.

**Current posture:** NeoVim stays forever unless both of those become true.

## Custom coding-agent harness

Claude Code is the best option today. In 12–18 months the agent landscape will have more options (OpenCode, PyAgent, custom harnesses).

**Design rule:** Phase 2's Claude Code frontend must be agent-agnostic at the IPC layer. Anything that speaks a prompt/response protocol over stdio or sockets can be plugged in without UI changes.

## The federation question, revisited

Today: monolith, because inter-app communication under Hyprland is primitive.

Once the custom WM exists: reconsider. A WM that ships a first-class IPC protocol could make a federation of Symmetria apps cleaner than a monolith. The rewrite to federation would not be wasteful at that point — it would be a natural consequence of having earned a better substrate.
