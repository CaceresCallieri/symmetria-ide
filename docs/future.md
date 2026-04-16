# Future

Things that are years out but influence today's decisions.

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
