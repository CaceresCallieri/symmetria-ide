---
name: startup-perf
description: "How to benchmark IDE cold start, and the two big costs found+fixed (browser-MCP GUI-thread import, eager WebEngine)"
metadata: 
  node_type: memory
  type: reference
  originSessionId: ff5a74c5-0a74-4855-b0ea-5268bea5dc7a
---

# IDE startup performance — how to measure + the costs found

**How to benchmark (reuse this):**
- `SYMMETRIA_IDE_TRACE=1` makes `trace.py` emit `[TRACE] <ms-from-process-start> <phase>` to stderr — the startup waterfall. Phases: `imports_basic_done → app_module_imported → run_entered → qgui_created → engine_ctx_ready → engine_loaded → backend_started → terminal_started → exec_entered → first_capsule`.
- Run the **ephemeral screenshot harness** so it exits on its own (no lingering window): `SYMMETRIA_IDE_TRACE=1 SYMMETRIA_IDE_SCREENSHOT=/tmp/x.png SYMMETRIA_IDE_WARMUP_MS=1500 SYMMETRIA_IDE_SETTLE_MS=80 PYTHONPATH=src python -m symmetria_ide 2>trace.log`.
- **Trust per-phase DELTAS over absolute times** — absolute times drift with machine load/thermal across a session; deltas within one run are drift-immune. For honest before/after, do an **interleaved git-stash A/B**: `git stash push -q <files>; run; git stash pop -q; run` in a loop (cancels drift).
- `python -X importtime -c "import symmetria_ide.app"` attributes module-import cost without launching the GUI.
- `bench/measure_mount.py --repo ~/work/sales/bambin --runs 5` is the file-tree-mount regression gate (run it after ANY surface-deferral change — deferring a surface can change first-frame scheduling; verify on bambin, the dominant large repo).

**Surprises worth remembering:**
- `QtWebEngineQuick.initialize()` is CHEAP (~20ms) — Chromium init is lazy until a `WebEngineView` exists. WebEngine's startup cost is the **import** (~120ms) + **eager QML instantiation** (~430ms), not `initialize()`.
- `first_capsule` (status-bar populated = true interactivity) is gated by the user's **nvim config boot** (~1.5s with plugins), which overlaps the Python path in nvim's child process. Once the Python path is fast, nvim becomes the floor.

**The two big costs found + fixed (2026-06-24)** — took cold-start `exec_entered` ~2.5s → ~1.0s, `first_capsule` ~2.5s → ~1.25s:
1. **Browser MCP server start blocked the GUI thread ~1s.** `BrowserMcpServer._start()` ran `from mcp.server.fastmcp import FastMCP` + `import uvicorn` synchronously in `AppController.start()` on EVERY launch — even though browser-agents are per-project default-OFF. Fix: start it **lazily on a daemon starter thread (`_start_thread`), gated on `_project_browser_enabled`** (only opted-in projects). See `src/symmetria_ide/browser_mcp.py` `start()`/`_start_guarded` + `AppController._refresh_project_browser_enabled`. A silent regression — added Phase 4 (June), after the May `bench/` rounds, never measured.
2. **Eager `BrowserSurface` cost ~430ms of `engine.load`** (the `import QtWebEngine` QML module + persistent `WebEngineProfile`). Fix: wrap it in a `Loader` gated on a one-way `controller.browserEverOpened` latch flipped on first `open_browser`. See `qml/Main.qml` `browserSurfaceLoader` + `AppController.open_browser`.

**Rule for future startup work:** never add a heavy import or eager heavyweight-QML-component to the pre-`app.exec()` path for a capability most launches don't use — defer it (Loader / lazy import / background thread) and gate on actual need. Related: [[mcp_enablement_per_project]] (the per-project gate this reused).
