---
name: qtwebengine_cdp_devtools_mcp
description: "QtWebEngine 6.11 exposes a near-complete CDP endpoint; the off-the-shelf chrome-devtools-mcp (Puppeteer-based) drives the embedded view fully, while Playwright fails on attach (#36961). Settles the agentic-browser \"build our own vs adopt a mature MCP\" question."
metadata: 
  node_type: memory
  type: reference
  originSessionId: c1882ebc-4b92-4b54-9e54-6d592472bb6f
  modified: 2026-07-27T05:16:33.859Z
---

> **SUPERSEDED 2026-07-27.** The embedded QtWebEngine browser was replaced by real external Google Chrome (`chrome_host.py`), and the CDP port env var was renamed `QTWEBENGINE_REMOTE_DEBUGGING` → `SYMMETRIA_IDE_CDP_PORT`. Everything below describes the embedded era; keep it for the CDP/Puppeteer-vs-Playwright findings, which still hold, but do not configure anything from it.

# QtWebEngine's CDP is complete enough to adopt chrome-devtools-mcp wholesale

**Verified by spike 2026-06-18** (scripts were at `/tmp/spike/`, ephemeral). The agentic browser does NOT need a hand-maintained MCP, nor a separate real-Chrome-streamed-in (screencast) architecture. The embedded `QtWebEngine` view already IS Chromium and exposes a near-complete Chrome DevTools Protocol endpoint.

## How
- Launch the embedded engine with env `QTWEBENGINE_REMOTE_DEBUGGING=<port>` set **before** QtWebEngine is imported. It then serves the standard `/json/version` + `/json` target list and per-page websocket CDP, exactly like Chrome's `--remote-debugging-port`.
- Point the real `chrome-devtools-mcp` at it: `npx chrome-devtools-mcp --browserUrl http://127.0.0.1:<port>` (or `--wsEndpoint ws://…`). **All 29 tools dispatch** through the MCP handshake against QtWebEngine 6.11: `take_screenshot` (returned a 49 KB image), `evaluate_script`, `list_network_requests`, `list_console_messages`, `performance_start_trace`, `take_snapshot`, input tools, etc.

## The one trap: Puppeteer works, Playwright does NOT
- `chrome-devtools-mcp` is built on **puppeteer-core**, whose `connect({browserURL})` attaches lazily and succeeds against QtWebEngine.
- **Playwright's `connect_over_cdp` FAILS** (microsoft/playwright #36961): it eagerly calls `Browser.setDownloadBehavior` on attach, which QtWebEngine answers "Browser context management is not supported", aborting the whole connection. So engine choice (Puppeteer vs Playwright) is the deciding variable — pick a Puppeteer-based MCP.

## CDP gaps in QtWebEngine 6.11 (all narrow, none block the headline features)
- `Page.printToPDF` → NOT implemented (Qt has native `QWebEnginePage.printToPdf` if needed).
- `Browser.setDownloadBehavior` → not supported.
- `Target.getBrowserContexts` / multi-browser-context → "Not allowed" (single default context only).
Everything else measured worked: screenshot, network + `getResponseBody`, console events, `Performance.getMetrics` (36 metrics), DOM, eval, input dispatch, emulation, **and `Page.startScreencast` emits frames** (so screencast is available if ever wanted — but unnecessary, since the view is already embedded/visible/contained).

## Implication for the IDE — SHIPPED as Stage 4 (2026-06-18)
Adopting `chrome-devtools-mcp` pointed at the embedded view gives **full feature parity with the top MCP AND keeps containment** (no escaped Wayland window, no screencast viewer to build). Shipped as the **HYBRID** in CLAUDE.md "The browser panes" Stage 4: CDP enabled via `QTWEBENGINE_REMOTE_DEBUGGING` in `app.run()`; `browser_mcp.agent_config_path` injects a `chrome-devtools` stdio entry alongside our http entry; we KEEP only `browser_open` + `browser_list_windows` (visible-window allocation + url↔window correlation — `chrome-devtools-mcp new_page`/`Target.createTarget` is "Not supported" on QtWebEngine, so it can't allocate a pooled view) and RETIRED the six driving tools + `BrowserAutomation` + the bridge op-path. **Two operational caveats confirmed live and load-bearing:** (1) render-dependent CDP ops (`take_screenshot`) STALL on an inactive Hyprland workspace (QtQuick render loop throttled) — work instantly when the window is composited; (2) the Stage-3 in-flight PULSE is deferred (chrome-devtools ops bypass our bridge; `_record_browser_attribution` is the dormant hook a future IDE-owned CDP monitor will re-activate). Ownership glyph + click-jump preserved. Live-verified end-to-end (377 KB screenshot on a visible+loaded page; select_page-by-url correlation; navigate-on-existing).
