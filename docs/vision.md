# Vision

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

- A wrapper that embeds NeoVim, hosts an agent frontend, and integrates the File Manager.
- A progressively extracted UI: NeoVim's chrome moves into native QML panels over time.
- Keyboard-first, Symmetria-aesthetic, opinionated.

## What this is NOT

- Not a replacement for NeoVim (NeoVim is the editor core, forever or until a gpui rewrite).
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
