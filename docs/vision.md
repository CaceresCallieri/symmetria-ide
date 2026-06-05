# Vision

> **Framework pivot (2026-06):** the *what* and *why* of this vision are unchanged; the *how* is moving from PySide6/QML to **Tauri 2 (Rust + React/TS)**, with NeoVim running in xterm.js and a long arc toward an own web editor. Sections below are updated where they named the QML implementation. Authoritative detail: `docs/framework-pivot.md`.

## The long horizon

A unified environment — *your own Emacs* — in which the daily developer workflow happens: editing, agent conversations, git, file navigation, browser-based agentic work, diagram rendering, image viewing.

**Time horizon:** 6–18 months to a meaningful Phase 2 completion. 1–1.5 years for a polished system. The pace tracks LLM advancement and available time.

## Why a new IDE

The current workflow is NeoVim in a terminal, Claude Code in another terminal, Symmetria File Manager floating separately, browser open in another Hyprland workspace, screenshots sent to Claude with only partial visual confirmation. The workflow is powerful but fragmented.

Specific limitations that drive this project:

1. **Image blindness.** Screenshots sent to Claude Code cannot be visually confirmed in the terminal. Workarounds give partial observability only.
2. **Diagram rendering.** Claude can emit rich HTML/CSS diagrams that the terminal cannot show. Opening a browser tab breaks flow.
3. **Agentic browser isolation.** Hyprland workspace isolation is bypassed by Chrome/Firefox security rules; agent-spawned browsers escape their intended workspace.
4. **File manager disconnection.** The File Manager opens at `~`, unaware of which project the focused NeoVim client is editing.
5. **Aesthetic ceiling.** The terminal's visual expressiveness caps what the workflow can feel like.

## What this IS

- A **wrapper** that embeds a swappable editor (NeoVim today, an own web editor long-term), hosts an agent frontend, and integrates the File Manager.
- Built on **Tauri 2 (Rust + React/TS)**; NeoVim runs in an xterm.js terminal with an `nvim --listen` side channel feeding the native web chrome.
- A progressively extracted UI: NeoVim's responsibilities (chrome, file tree, which-key, git) move into native **web** surfaces over time, until NeoVim is "just a buffer" and is replaced.
- Keyboard-first, Symmetria-aesthetic (recreated in CSS), opinionated.

## Modes of inhabiting the IDE

The IDE serves two distinct usage modes; both must feel native, and the transition between them is **implicit, not modal** — there is no "mode switch" command.

### Navigation mode

Drifting around the filesystem. Light edits. No commitment to any particular project. The user is `cd`-ing through directories in the terminal, glancing at files briefly, popping into the file manager, asking the agent a one-shot question. State is ephemeral — closing the IDE loses nothing of value.

This is the **unanchored** state. Terminal cwd drives `displayedRoot`, the file tree follows the terminal, no project root is pinned. Quick edits land in scratch buffers or one-shot file opens; no session bookkeeping happens.

### Project mode

The user has committed to a project. They want IDE state preserved — open buffers, terminal layout, agent conversation — so they can return to exactly the same workspace later (in the same launch, and eventually across launches).

This is the **anchored** state. The anchor pins a project root, `displayedRoot` stops following the terminal's cwd, and git operations target the anchored root. Anchoring is the user-visible face of "I'm working on this thing now."

### Why both matter

A pure navigation tool (terminal + FM, no anchor concept) loses context every time the user wanders. A pure project IDE ("open project" required before anything works) is heavyweight for the 80% case of "I just want to look at this file." Symmetria IDE refuses to force the choice: navigation mode is the default, project mode is what you opt into when the work earns it.

### Future direction

The anchor concept is the seed of a future **session model**: opening a project rehydrates persisted state (nvim buffers via `:mksession`, terminal cwd, agent SDK `resume`-able conversations); closing a project snapshots it back. Today we have the anchor primitive but not the persistence layer — that's a Phase 3+ deliverable. The dual-mode framing here is what keeps the eventual session work from feeling like a bolt-on; it's the natural extension of a concept that already lives in the codebase.

## Surface hierarchy (evolving)

The IDE houses four distinct surfaces with different roles. Their relative prominence is **expected to invert** over the project's lifetime as agent capabilities mature.

| Surface       | Role                                  | Today (Phase 0–2)                                     | Long-term direction                              |
|---------------|---------------------------------------|-------------------------------------------------------|--------------------------------------------------|
| **Agent**     | *Action* — how the user directs work  | Summoned over the editor via `<leader>aN`             | Always-on, default-visible primary surface       |
| **Observation** | File tree + git status + (later) file manager — how the user *sees* project state | Always-on sidebar to the editor's right             | Unchanged — always-on, expands to also include diff views, build status, etc. |
| **Navigation** | Terminal — how the user moves between projects and contexts | Built (Phase 2.5 — shipped 2026-05-18)             | Persistent home surface; cwd drives the observation pane until anchored |
| **Edit & view** | NeoVim (in xterm.js) → own web editor — opening buffers to read or hand-edit | Central pane on launch; default focus              | Summoned on demand; eventually an own web editor, not the launch state |

The chrome surfaces (agent, observation, navigation) are **native web (React) panels**; the editor is **NeoVim rendered in an xterm.js terminal**, with its data-side chrome (status/branch/cwd) fed over an `nvim --listen` RPC channel. The **direction of travel** is from an editor-centric IDE (NeoVim as the hub, agent/file-tree as satellites) toward an *agent-and-navigation-centric* IDE (terminal as the launch state, agent as the primary action surface, file tree + git as always-on observation, the editor summoned on demand).

This is a multi-year direction, not a near-term refactor. Each Phase 2+ deliverable should be evaluated against whether it composes cleanly with the long-term topology — not whether it implements it. See `docs/future.md` for the longer-form discussion.

## What this is NOT

- Not a replacement for NeoVim *during the transition* — NeoVim remains the real editing core (the user's actual config) until the IDE is hollowed it out enough that an own web editor with vim-style navigation can take over. The long-term plan *does* eventually retire NeoVim (a shift from the old "NeoVim forever or until gpui" framing — see `docs/framework-pivot.md` §8).
- Not a general-purpose IDE for others. Personal tooling.
- Not a monolith that swallows the whole Symmetria ecosystem. WhatsApp stays standalone. Future messaging apps stay standalone.

## Ecosystem boundaries

| Component                    | Inside IDE? | Why |
|------------------------------|-------------|-----|
| Symmetria Shell              | No          | Different scope (desktop shell). |
| Symmetria File Manager       | **Yes**     | Fast file reference to agent is a hot path. |
| Symmetria WhatsApp           | No          | Standalone app, separate logic. |
| Future Discord / mail apps   | No          | Same reasoning as WhatsApp. |
| `orchestrator.nvim`          | Absorbed    | Its capsules surface in the native status bar. |
| Agentic browser              | **Yes**     | Solves the Hyprland workspace escape + enables direct agent↔browser control. |

## Success criteria

The project has succeeded (at its first meaningful checkpoint) when:

1. Daily coding — editing, agent interaction, git, file navigation — happens inside one IDE window.
2. Images and HTML diagrams from Claude Code render inline, without leaving the window.
3. An agent can operate a browser without breaking out of the IDE.
4. Keyboard latency feels indistinguishable from stock NeoVim in a terminal.
5. The aesthetic matches Symmetria Shell and File Manager without effort.
