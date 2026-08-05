---
name: startup-optimization-followups
description: IDE cold-start optimization outcomes — gc.collect drop SHIPPED; WebEngine import deferral SPIKED+HELD (Qt-deprecated late init). Method + baseline + why-held.
metadata: 
  node_type: memory
  type: project
  originSessionId: ff5a74c5-0a74-4855-b0ea-5268bea5dc7a
---

# Startup optimization — follow-up outcomes (resolved 2026-06-25)

Record of the two follow-up cold-start optimizations, both now resolved. Companion to the methodology/pitfall note [startup-perf](../../reference/qt-pyside/startup_perf.md). Read that first for the benchmarking method.

**Outcome at a glance:**
- **Opt A (drop `gc.collect()`): SHIPPED** 2026-06-25, commit `51bf26c`. ~20–28ms. See below.
- **Opt B (defer WebEngine import): SPIKED, WORKS, but HELD** by user decision 2026-06-25 — it structurally requires a Qt-**deprecated** late `initialize()`. Do NOT re-attempt without a non-deprecated lazy-init path. See the full why-held below.

## Status — what already shipped (don't redo)

Two fixes landed 2026-06-24 on `dev` (commits `36d89f8` perf + `d262f76` review-hardening), taking cold-start `exec_entered` ~2.5s → ~1.0s and `first_capsule` ~2.5s → ~1.25s (interleaved git-stash A/B):
1. **Browser MCP server**: was a ~1s synchronous `from mcp.server.fastmcp import FastMCP` + `import uvicorn` on the GUI thread in `AppController.start()` on EVERY launch. Now starts lazily on a daemon starter thread (`BrowserMcpServer._start_thread`), gated on the per-project browser opt-in via `AppController._refresh_project_browser_enabled`.
2. **BrowserSurface (QtWebEngine QML)**: was eagerly instantiated (~430ms of `engine.load`). Now behind a `Loader` in `Main.qml` (`browserSurfaceLoader`) gated on the one-way `controller.browserEverOpened` latch (flipped in `AppController.open_browser`).

Then 2026-06-25, commit `51bf26c`: **Opt A (gc.collect drop)** — see its section below.

**Investigated and REJECTED — do not pursue:** lazy-spawning the editor nvim. Measured nvim boot is only ~228ms TUI (~115ms headless; clean nvim ~9ms + user config ~99ms + IDE runtime injection only ~7ms), and it OVERLAPS `engine.load` (nvim forks in `Component.onCompleted`, boots in its child process). It contributes only a ~150–250ms non-overlapped tail to `first_capsule` (the always-visible status-bar editor fields). Deferring it would save only that tail, only on editor-less launches, at the cost of an empty status bar + a ~228ms delay on first editor-open. Bad trade. nvim is NOT the bottleneck.

## Current baseline (post-fix, warm, this machine)

| Phase | ~cost | Notes |
|---|---|---|
| `app_module_imported` | ~500–600ms | PySide6 core (~64ms, irreducible) + **WebEngine import chain ~120ms** + app.py body ~94ms + pynvim ~32ms + rest |
| run → `engine_ctx_ready` | ~35ms | surface format, CDP probe, `QtWebEngineQuick.initialize()` (cheap ~20ms), QGuiApplication, controller + ctx props |
| `engine.load(Main.qml)` | ~370ms | two `QMLTermWidget` C++ instantiations (editor+shell KSession) + FM module tree + design singletons |
| `start()` | ~10ms | backend.start (non-blocking) + bridge + sync emit |
| start_done → `exec_entered` | ~57ms | **`gc.collect()` + `gc.freeze()`** ← Optimization A |
| exec → `first_capsule` | ~150–250ms | nvim TUI boot tail + RPC attach/subscribe handshake (gotcha #2) |

Total to `exec_entered` ≈ 1.0s; to `first_capsule` ≈ 1.25s. The two optimizations below target ~170ms combined (the only remaining low/medium-risk levers; everything else is irreducible framework cost).

## Benchmarking method (reuse EXACTLY — drift-controlled)

Per-phase deltas within one run are drift-immune; absolute times drift with machine load, so for honest before/after use an **interleaved git-stash A/B**:
```sh
runone() { SYMMETRIA_IDE_TRACE=1 SYMMETRIA_IDE_APP_ID=symmetria-ide \
  SYMMETRIA_IDE_SCREENSHOT=/tmp/x.png SYMMETRIA_IDE_WARMUP_MS=1500 SYMMETRIA_IDE_SETTLE_MS=80 \
  PYTHONPATH=src python -m symmetria_ide 2>/tmp/ab.log >/dev/null
  grep '\[TRACE\]' /tmp/ab.log; }
for i in 1 2 3; do git stash push -q <files>; runone; git stash pop -q; runone; done
```
The screenshot harness is EPHEMERAL (exits on its own — no lingering window). Trace markers: `imports_basic_done → app_module_imported → run_entered → qgui_created → engine_ctx_ready → engine_loaded → backend_started → terminal_started → exec_entered → first_capsule`. Also `python -X importtime -c "import symmetria_ide.app"` for import attribution. Always: `QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/ -q` (910 tests) + ruff before committing.

---

## Optimization A — drop the redundant `gc.collect()` — ✅ SHIPPED (commit `51bf26c`)

**Shipped 2026-06-25.** Measured (not assumed): the collect cost **~20ms** (not the ~50ms first estimated), and a startup probe found **~0 unreachable objects** at the freeze point (0 on most runs, 24 once) with flat RSS — so collect-before-freeze was reaping essentially nothing. Interleaved A/B: the `terminal_started→exec` phase fell from ~19–29ms to ~0.7ms. `gc.freeze()` still freezes the whole live set, so the gotcha-#10 GC-vs-render mitigation is unchanged. The removed line carries a comment block documenting WHY it was dropped (so it isn't "restored" as a perceived regression) and pointing at `gc.collect(0)` as the conservative fallback if startup ever allocates real cyclic garbage. 910 tests pass; no crash.log SEGV relapse.

**Original spec (for reference) — Location:** `src/symmetria_ide/app.py` in `run()`, immediately before `app.exec()`:
```python
gc.collect()  # ← the ~20ms cost (removed)
gc.freeze()  # ← kept
```

**Why it's likely redundant:** `gc.freeze()` moves all currently-tracked objects into a permanent generation never scanned again (the gotcha-#10 mitigation — shrinks the "GC runs while QSGRenderThread paints" SEGV surface under Python 3.14). The preceding `gc.collect()` only serves to free cyclic garbage so it isn't frozen-in. Dropping it saves the full-collection cost (~50ms measured as part of the 57ms start_done→exec phase) but **freezes whatever garbage exists at that point into the permanent gen** = a small one-time memory bump held until process exit.

**Risk: LOW.** Dropping the collect does NOT reduce gotcha-#10 safety (freeze still freezes the live set; garbage also stays un-collected → render-race surface unchanged/smaller). The only downside is the frozen-garbage memory bump. For a desktop IDE this is almost certainly KB–low-MB and acceptable.

**Implementation:**
1. FIRST measure the actual `gc.collect()` cost to confirm ~50ms (don't assume): wrap it with `t=time.monotonic()` / `trace`-style timing, or just A/B the whole change. If it's <15ms, the optimization isn't worth the memory tradeoff — reconsider.
2. Replace the two lines with just `gc.freeze()`, and REWRITE the comment block above (4574-4582) to explain WHY the collect was dropped and the frozen-garbage tradeoff (so a future reviewer doesn't "restore" it as a bug — this is exactly the Regression-Documentation discipline in the global CLAUDE.md).
3. Consider the middle-ground fallback if paranoid: `gc.collect(0)` (youngest gen only — much faster than a full collect, still frees most startup garbage). Mention in the comment as the conservative alternative.

**Validation:**
- Interleaved A/B on `exec_entered` (expect ~50ms improvement on the start_done→exec phase).
- Quantify frozen garbage: print `len(gc.garbage)` + process RSS (e.g. `psutil`/`/proc/self/status` VmRSS) just after `app.exec()` start, with vs without the collect — confirm the bump is small.
- Full pytest suite + one real launch (watch `$XDG_STATE_HOME/symmetria-ide/crash.log` for any gotcha-#10 SEGV relapse — there should be none; freezing is the safe direction).

---

## Optimization B — defer the WebEngine import — ⛔ HELD (spiked, works, but Qt-deprecated)

> **DECISION 2026-06-25 (user): HOLD it, documented. Do NOT re-implement without a non-deprecated lazy-init path.** The optimization is real and works on the current Qt — but it structurally depends on a Qt-**deprecated** API call, and the user chose not to take on that future-Qt-bump risk for the win. The eager init stays.

**What was verified (so nobody re-spikes from scratch):**
- **The spike PASSED.** A standalone script (approach in the "make-or-break question" spec below) set `AA_ShareOpenGLContexts` by hand BEFORE `QGuiApplication`, did NOT import WebEngine up front, started the event loop, then in a deferred tick imported `QtWebEngineQuick` + called `initialize()` + loaded a `data:` page in a `WebEngineView`. Result: `initialize()` returned cleanly and WebEngine **rendered real pixels** (center-pixel sampled magenta; screenshot confirmed crisp page). So deferred init is *functionally* correct on Qt 6.11.
- **Measured win: ~70–120ms** off `app_module_imported` (interleaved A/B; B steady ~411–434ms vs A's ~482–552ms). `importtime` with `PYTHONPATH=src` confirmed `QtWebEngineQuick` no longer loads at startup. (Gotcha: a bare `import symmetria_ide.app` resolves the **stable** worktree's editable install, which still eager-imports — always pin `PYTHONPATH=src` when measuring dev.)
- **The blocker — Qt deprecation.** Wiring it into the real app and opening a browser, Qt logged: `QtWebEngineQuick::initialize() called with QCoreApplication object already created and should be call before. This is deprecated and may fail in the future.` Deferring the *import* and avoiding this warning are **mutually exclusive**: the import is the ~120ms cost, the import must precede `initialize()`, and `initialize()`'s only hard pre-`QGuiApplication` job is `AA_ShareOpenGLContexts` (which we *can* set by hand) — so to move the import past `QGuiApplication` you MUST also move `initialize()` past it, which is exactly the deprecated path. No clean workaround (backgrounding the `.so` load / a post-first-frame pre-warm both still call `initialize()` late → same deprecation, or add fragility for no net win).
- **If ever revisited:** the rollback/guard is trivial (it's the *current* eager state). Re-attempt ONLY if a future Qt ships a supported lazy/late `initialize()` (watch the deprecation’s fate across Qt bumps; removal likely lands at Qt 7). Any attempt MUST end with a live browser render check on an **active, composited** workspace — the screenshot harness canNOT verify embedded WebEngine content (its `basic`-loop `grabWindow()` captures QML chrome but not Chromium's GPU-composited layer on a background window; the chrome rendered while the page area stayed blank in every harness run, a capture limitation, not a render failure — the standalone spike is the valid render proof).

**Original spec (kept for the reasoning trail / a future Qt where late init is supported):**

**The cost:** `src/symmetria_ide/app.py:51` `from PySide6.QtWebEngineQuick import QtWebEngineQuick` at module top loads `libQt6WebEngineCore.so` + its dependents (QtWebEngineCore, QtWidgets, QtPrintSupport, QtOpenGL, QtNetwork) ≈ ~120ms of `app_module_imported`. CONFIRMED nothing else in `src/symmetria_ide/*.py` imports those modules, so they are 100% WebEngine — fully recoverable IF WebEngine can be deferred.

**The hard coupling (why this is all-or-nothing):**
- `QtWebEngineQuick.initialize()` (app.py:4497) must run BEFORE `QGuiApplication(...)` (app.py:4502) — it installs `AA_ShareOpenGLContexts` the Chromium compositor needs.
- CLAUDE.md "browser panes": calling `initialize()` AFTER the QML engine loads `import QtWebEngine` "is too late and throws."
- `initialize()` needs the module imported. So the import can't move later than `initialize()`, and `initialize()` can't move later than `QGuiApplication` — UNLESS we break the coupling (below). Merely moving the import from module-top into `run()` saves ~0 wall-clock (it still runs at startup). **So Optimization B only pays off if `initialize()` can run lazily at first-browser-open.**

**The make-or-break question (SPIKE THIS BEFORE ANY REAL WORK):** can `QtWebEngineQuick.initialize()` be called AFTER `QGuiApplication` exists and the event loop is running (i.e., at first `open_browser`), provided `AA_ShareOpenGLContexts` was set manually before `QGuiApplication`?
- The `BrowserSurface` Loader already defers `import QtWebEngine` to first open (shipped). So the plan is: in `open_browser`, BEFORE flipping `browserEverOpened`, call `QtWebEngineQuick.initialize()` (importing the module lazily there), THEN flip the latch (Loader activates → `import QtWebEngine` resolves against the now-initialized engine).
- Spike script (standalone, ~40 lines, model on the verified `/tmp/browser_smoke.py` pattern from the session): set `QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)` before `QGuiApplication`; do NOT import/initialize WebEngine up front; start the event loop; in a `QTimer.singleShot`, `from PySide6.QtWebEngineQuick import QtWebEngineQuick; QtWebEngineQuick.initialize()`, then instantiate a `WebEngineView` on `https://example.com` and grab a screenshot. If it renders the chrome + navigates with no fatal error → lazy init works → proceed. If it throws / blank-crashes → **abandon Optimization B** (the ~120ms is irreducible) and document that.
- Must run on the LIVE Hyprland session (not `offscreen`) — WebEngine render needs a composited surface (same caveat as the shipped browser smoke).

**If the spike passes — implementation:**
1. Set `QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)` before `QGuiApplication` in `run()` (replaces what `initialize()` did early).
2. Remove the module-top `from PySide6.QtWebEngineQuick import QtWebEngineQuick` (app.py:51) and the early `QtWebEngineQuick.initialize()` (4497). Keep the QSurfaceFormat alpha block + the CDP `QTWEBENGINE_REMOTE_DEBUGGING` env reservation early (those are cheap and the env var is consumed by Chromium at `initialize()` time whenever that runs).
3. Add a lazy one-shot initializer (e.g. `AppController._ensure_webengine_initialized()` with an idempotent flag) that does the deferred `import` + `QtWebEngineQuick.initialize()`, called at the TOP of `open_browser` BEFORE `browserEverOpenedChanged` is emitted (so the engine is up before the Loader imports `import QtWebEngine`).
4. Update CLAUDE.md "browser panes" + the `QtWebEngineQuick.initialize()` ordering notes + `docs/dev-workflow.md` to describe the lazy-init model (the current docs assert it MUST be before QGuiApplication — that becomes conditional).

**Risk: MEDIUM-HIGH.** Qt-version-sensitive (verified target: PySide6 6.11.1 / Qt 6.11). The shared-GL-context timing is the classic failure mode (blank/garbled WebEngine, or a hard crash). If it regresses on a Qt bump, the symptom is the embedded browser failing to render. Gate the whole thing behind the spike; if uncertain after the spike, DON'T ship it — ~120ms isn't worth a flaky browser.

**Validation:** the spike itself + the shipped browser smoke (`open_browser` → renders example.com chrome+addressbar, no QML errors) + interleaved A/B on `app_module_imported` (expect ~120ms drop) + confirm `python -X importtime` no longer shows QtWebEngineQuick at startup + a live `Ctrl+T` / agent `browser_open` end-to-end on the real compositor + `hyprctl clients` shows no escaped Chromium window.

## Key file/line references (verify — they drift)

- `src/symmetria_ide/app.py:51` — WebEngine module-top import (Opt B).
- `src/symmetria_ide/app.py:4447-4449` — QSurfaceFormat alpha block (keep).
- `src/symmetria_ide/app.py:4473-4483` — CDP `QTWEBENGINE_REMOTE_DEBUGGING` reservation (keep early).
- `src/symmetria_ide/app.py:4497` — `QtWebEngineQuick.initialize()` (Opt B moves this lazy).
- `src/symmetria_ide/app.py:4502` — `QGuiApplication(...)` (the before/after boundary).
- `src/symmetria_ide/app.py:4583-4584` — `gc.collect()` / `gc.freeze()` (Opt A).
- `src/symmetria_ide/app.py` `open_browser` / `browserEverOpened` — the lazy-init hook point (Opt B step 3).
- `qml/Main.qml` `browserSurfaceLoader` — already defers `import QtWebEngine` (shipped).

**Both resolved 2026-06-25:** Opt A shipped (`51bf26c`); Opt B spiked → works → HELD (Qt-deprecated late init). The two remaining low/medium-risk cold-start levers are now exhausted — further startup wins are in irreducible framework cost (PySide6 core import ~64ms, the two `QMLTermWidget` C++ instantiations in `engine.load`, nvim TUI boot tail) unless a dependency changes (e.g. a Qt that supports lazy WebEngine init reopens Opt B).
