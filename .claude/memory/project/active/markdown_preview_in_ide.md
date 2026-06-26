---
name: markdown-preview-in-ide
description: "Future idea — render a Markdown preview inside the IDE's embedded browser, themed to match, toggled by a key"
metadata: 
  node_type: memory
  type: project
  originSessionId: 32500152-2a8e-4fff-86ef-818a8c2023a0
---

**Idea (future, NOT in flight — surfaced 2026-06-21):** add a Markdown *preview*
surface to Symmetria IDE that renders the file open in the embedded editor as
full HTML **inside the IDE's own embedded browser**, toggled by a keybind, and
themed with `Theme.*` tokens so the preview looks visually identical to the rest
of the IDE chrome.

This is distinct from in-buffer rendering (render-markdown.nvim, just added to
the user's standalone nvim config): in-buffer styling lives in the editor grid;
this idea is full-fidelity HTML (Mermaid, KaTeX, real tables) in a browser pane,
live-reloading as the buffer changes.

**Mechanism (compose, don't reimplement — non-negotiable #4):** browser
markdown-preview tools (e.g. `markdown-preview.nvim`) serve a live-reloading
**localhost HTTP page**. The IDE already owns the pieces to embed that:
- an embedded `WebEngineView` pool (`qml/BrowserSurface.qml`) — point a window at
  the localhost preview URL instead of letting it escape to a Hyprland top-level
  (the same containment principle the browser panes were built on).
- the Python↔nvim RPC seam that already knows the editor's current file (the
  `file` capsule) and cwd — drive the preview's target from there.
- browser chords + the agent-owned browser topology to hang a preview window off.

The differentiator: because the IDE controls the preview HTML/CSS, it can be
themed to `Theme.*`, so Markdown looks the same whether rendered in-buffer or as
HTML — a cohesive feature no off-the-shelf editor has.

**Why:** the user wants Markdown to "look the same as the browser by pressing a
key" inside the IDE; it's a natural extension of the embedded-browser work and
reuses existing surfaces rather than adding a new rendering engine.

**How to apply:** treat as a candidate extension when revisiting Phase 4 browser
work (`docs/phases.md` Phase 4, `docs/future.md`). Prior art lives in the
browser-panes section of CLAUDE.md and
[QtWebEngine CDP](../../reference/qt-pyside/qtwebengine_cdp_devtools_mcp.md). A
user-triggered preview needs no MCP gate; if any piece ever becomes agent-driven,
gate it per [per-project MCP enablement](../../feedback/mcp_enablement_per_project.md).
