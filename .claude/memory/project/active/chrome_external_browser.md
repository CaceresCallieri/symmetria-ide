---
name: chrome-external-browser
description: The agentic browser is external Chrome pinned by a Hyprland rule (shipped 2026-07-27); the in-window form is the next step via a nested Wayland compositor
metadata: 
  node_type: memory
  type: project
  originSessionId: 1dc16f14-6524-437a-9b81-8d0fde68876c
  modified: 2026-07-27T04:33:53.374Z
---

# Agentic browser: external Chrome now, in-window next

Architecture, invariants and gotchas live in CLAUDE.md "The browser panes" and
in `chrome_host.py` / `hyprland_ipc.py` / `cdp_client.py`. This file carries only
what those don't: where the work stands and what was decided.

**Shipped 2026-07-27** (commit "replace the embedded QtWebEngine pool with real
Google Chrome"). QtWebEngine is gone — it could not do the job (`new_page`
unsupported, screenshots stalling off-workspace, no extensions, no real logins).

**User decisions, so don't relitigate them:**

- **Full Chrome UI, not `--app=`.** The browser doubles as a surface for showing
  things to other people, so tabs and the omnibox are wanted.
- **Notify, don't yank.** `Ctrl+Shift+B` and the chip globe toast; they do not
  focus the window. Raising an external window means a workspace switch the user
  did not ask for.
- **Profile: template + clone.** They pushed back on per-project profiles
  wanting one shared login. It is architecturally impossible — Chrome is a
  singleton per `--user-data-dir`, so a shared profile means one process, one
  class, one workspace (and, later, one nested compositor). Seeding from a
  `_template` profile was the accepted answer.

**Next step — the in-window browser.** Verified feasible live: real Chrome
renders as a `ShellSurfaceItem` inside a PySide6 window via
`QtWayland.Compositor`, which needs no Python bindings (the QML plugin loads
into any QML engine, same as QMLTermWidget). `ChromeHost` is deliberately
backend-agnostic so this swaps only where the surface lands.

Open items for it, in order of how much they'd cost:

1. **Clipboard is isolated both ways** — needs a ~30-line C++ compositor
   subclass. Measured, with the exact API:
   [nested-compositor-clipboard](../../reference/qt-pyside/nested_compositor_clipboard.md).
2. **No `zwp_linux_dmabuf_v1`** in Arch's `qt6-wayland` build, so Chrome falls
   back to `wl_shm` (CPU buffer copies per frame). Rendered fine in the spike;
   unmeasured under video/animation load.
3. **Fractional scaling** — the monitor runs `scale 1.6` and the nested output
   advertises no `wp_fractional_scale_v1`. `wp_viewporter` IS advertised, so
   `WaylandOutput.scaleFactor` + viewporter is the likely fix.
4. **Chord precedence** — unverified that `Qt.ApplicationShortcut` still beats a
   focused `ShellSurfaceItem` the way it beats QMLTermWidget.

**E2E verified 2026-07-27** (live IDE on ws 6, agent-shaped MCP client with the
attribution header, then chrome-devtools-mcp against the same Chrome): windows
born on the IDE's workspace without touching the user's; registry carrying live
titles/urls from CDP discovery; `browser_request_attention` accepted; `new_page`
working (it returned "Not supported" on QtWebEngine) and `take_screenshot`
succeeding **while Chrome sat on a non-composited workspace** — the exact case
that used to stall. Killing the IDE closed Chrome and released the window rule.

One thing the E2E taught that unit tests could not: a registry "window" is a CDP
**page target**, and `new_page` opens a TAB (it doesn't pass `newWindow`), so
target count and window count diverge.

**v1 gaps of the shipped pinned version:** windows created directly by an agent
through chrome-devtools-mcp (`new_page`) are unattributed (needs a
`Target.targetCreated` monitor); already-open windows don't follow the IDE
across workspaces (the rule acts at map time only).
