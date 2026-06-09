# Future

Things that are years out but influence today's decisions.

> **Framework pivot REVERSED (2026-06-07):** a Tauri 2 (Rust + React/TS) pivot was decided 2026-06-05 then reversed — the IDE stays on **PySide6/QML**. The topology-inversion and NeoVim-reclamation arcs below stand on their own (they were never pivot-dependent); read every "native QML" reference below as **native QML**, the live stack. `docs/framework-pivot.md` is a superseded historical record. The far-future editor-core *engine* (web vs native) is unsettled post-reversal — see "Own editor core" below.

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

**What this is NOT:** a plan to remove NeoVim. NeoVim remains the editor core through the transition (see the "Own editor core" section below for the declared end-state). The change in *this section* is in *prominence and default visibility*, not in *which tool does the editing* during the transition. When the user needs to hand-edit, NeoVim is the tool. The question is only how often that need arises and how it's surfaced.

## NeoVim reclamation roadmap

A direct consequence of the topology inversion above: capabilities historically routed through NeoVim plugins (because a terminal couldn't host anything else) progressively migrate into native Symmetria UI. This is **not a hostile takeover** during the transition — NeoVim remains the text-editing core until the own-editor end-state is reached (see "Own editor core" below; engine web-vs-native unsettled post-reversal). It's a redistribution of *responsibilities* that no longer have to live inside the editor surface now that the IDE owns a real GUI.

**Reclaimed (already shipped):**

- **Agent / Claude Code surface.** Previously `orchestrator.nvim` driving a NeoVim-floated TUI; now the native QML agent pane backed by the Node SDK sidecar (Phase 2). The Lua orchestrator is slated for deprecation once the IDE meets the gates in `docs/agent-dashboard-integration.md`.
- **Status line.** Previously `lualine`; now native QML status bar driven by the capsule protocol (Phase 0).
- **Which-key overlay.** Previously `which-key.nvim`'s in-terminal popup; now a native QML overlay driven by our own trie + state machine (gotchas #15–#21), with `which-key.nvim` neutralized on VimEnter. Live (the Tauri pivot would have re-conceived this as a web command gate and deleted the Lua which-key — that pivot was reversed, so the Lua which-key stack stays).
- **Cmdline + completion popup.** Previously plugin-drawn (`nvim-cmp`/`wilder`) floating at the wrong position once `ext_cmdline` was extracted (gotcha #8); now a custom `getcompletion()` pipeline feeding a native QML cmdline overlay (`CommandLine.qml`). Live.
- **File manager pane.** Symmetria File Manager hosted as a central pane (Phase 1 partial). Replaces ad-hoc nvim file pickers for project-tree browsing.

**Queued (near-term):**

- **Fuzzy file finder.** Currently `fff.nvim` inside NeoVim. The File Manager already has a basic fuzzy match primitive that needs hardening and broadening. Plan: a single native fuzzy finder reachable from the FM pane, from the terminal (as a callable), and eventually from nvim (replacing the leader binding). The point is **one search index, one ranking algorithm, one keyboard model** — invocable from any surface. The current per-pane patchwork is exactly the kind of nvim dependency the reclamation thesis targets.
- **Git operations surface.** Currently a mix of leader bindings, fugitive-style commands, and the existing git status capsule. Long-term direction: a native QML git pane with stage / unstage / diff / commit inline, reducing the `:G` surface area to "git operations from outside the editor."

**Far future (post-WM):**

- **Edit buffer itself.** The long-arc end-state (see "Own editor core" below): strip NeoVim until it is "just a buffer," then replace it with an own editor core — engine (web vs native) unsettled post-reversal. NeoVim remains the editing core through the full transition.

**Design rule for each reclamation:** the native Symmetria surface must be reachable from *every* pane (terminal, agent, FM, editor) with a uniform keybind. If we ship a fuzzy finder that only works when nvim has focus, we've failed — that's nvim-plugin territory, not a reclamation. The whole point is to make these capabilities IDE-wide primitives, not pane-local features. This rule is what makes the eventual topology inversion painless: when the agent pane becomes primary and the editor becomes summoned-on-demand, every reclaimed capability still works because none of them depend on the editor having focus.

## Own window manager

**Estimated timeline:** ~2 years.

**Plan:** fork Hyprland, evolve a custom Wayland compositor tuned to Symmetria. AI-assisted code work may make this feasible by 2028.

**Why it matters now:** owning the WM layer enables native inter-application protocols. At that point the IDE's monolithic shape becomes re-examinable — some current internal panes (File Manager, browser) could become standalone apps again, talking to the IDE via the compositor.

This is why today's monolith decision is **current, not permanent**.

## gpui migration — far-future full-rewrite candidate

**Target:** a possible far-future rewrite of the IDE in Rust on top of `gpui` (Zed's engine), once gpui has a stable public API.

**Status (post-reversal):** the 2026-06 Tauri 2 (Rust + React/TS) rewrite was considered as the rewrite target instead of gpui, then reversed — the IDE stays on PySide6/QML. So gpui returns to its prior standing: the leading far-future full-rewrite candidate, not a near-term plan, pursued only if the cost/benefit ever justifies leaving Qt/QML. The Tauri evaluation's findings (WebKitGTK substrate risk on Linux/Wayland, the RAM wash, the QML-codegen tax) are preserved in `docs/framework-pivot.md` §9.

**What Phases 0–2.5 in PySide6 bought:** they taught us exactly what surfaces, layouts, and event flows we want — so any future rewrite begins from a working reference, not a blank page.

## Additional Symmetria apps

Possible, all **standalone** (not absorbed into the IDE):

- Symmetria WhatsApp — in progress, about to replace the main WhatsApp instance.
- Symmetria Discord frontend — possible.
- Symmetria mail frontend — possible.

These share the aesthetic and the Symmetria identity but do not share a process with the IDE.

## Own editor core — long-arc end-state

The declared end-state of the editor arc: progressively strip capabilities out of NeoVim (file tree → shared file searcher → lazygit → orchestrator/agent logic) until NeoVim is "just a buffer visualization," then **replace it with an own editor core** carrying vim-style navigation (flash-like motions) and a few ergonomic plugins.

**Engine — unsettled post-reversal:** the reversed Tauri pivot had concretely promoted a *web* editor (Monaco / CodeMirror 6). With the pivot reversed and the IDE staying on Qt/QML, whether the eventual editor core is web (e.g. embedded via QtWebEngine) or native (Qt/QML) is an open decision. The *intent* to move off the terminal-based editing core survives the reversal; only the engine choice is reopened.

**Why the intent stands:** the user explicitly wants to move off the terminal-based editing core over time, and the AI-codegen + library ecosystem makes building a custom editor tractable. This is the concrete meaning of identity principle #5 ("progressive extraction toward an own editor").

**Posture:** NeoVim stays as the *real* editing core through the whole transition; it is retired only once the replacement genuinely matches the stripped-down editing role. This is a deliberate change from the old "NeoVim forever" posture.

## Custom coding-agent harness

Claude Code is the best option today. In 12–18 months the agent landscape will have more options (OpenCode, PyAgent, custom harnesses).

**Design rule:** Phase 2's Claude Code frontend must be agent-agnostic at the IPC layer. Anything that speaks a prompt/response protocol over stdio or sockets can be plugged in without UI changes.

## The federation question, revisited

Today: monolith, because inter-app communication under Hyprland is primitive.

Once the custom WM exists: reconsider. A WM that ships a first-class IPC protocol could make a federation of Symmetria apps cleaner than a monolith. The rewrite to federation would not be wasteful at that point — it would be a natural consequence of having earned a better substrate.
