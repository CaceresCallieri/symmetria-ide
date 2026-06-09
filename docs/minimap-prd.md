# PRD — Editor minimap

> **Status: Phase 0 shipped (2026-05-31).** Phase 0 surface skeleton landed in commit 142384c — empty `MinimapView` painted, QML slot + scroll wiring in place. Phase 1 (full-buffer content channel) is the next phase. This document is the planning anchor — written so a future session, including one running on a freshly-compacted context, can pick up at any phase without re-deriving prior decisions.
>
> **⚠ qmltermwidget migration (2026-06-08) — editor substrate changed.** This PRD was written against the deleted custom NeoVim grid renderer (`nvim_view.py`). Its references to `nvim_view.py` patterns (the scroll spring, the `_rgb_to_qcolor` memoization, specific line numbers) and to **gotcha #11** (smooth-scroll geometry, now retired) describe code that no longer exists. The editor is now a forked `QMLTermWidget` that renders itself, so the minimap must source buffer content from the Lua `minimap` rpcnotify channel (`runtime/lua/orchestrator/minimap.lua`) and its scroll position from the `QMLTermWidget` — that reconnection is the "pending viewport reconnection" noted in CLAUDE.md's source layout. Recover any referenced paint/spring patterns from git history (pre-migration commits). The phased plan and visual design below remain valid; only the editor-substrate plumbing changed.

A phased plan for adding a VS Code / Zed-style minimap to the embedded NeoVim editor surface — a narrow right-side column that shows a zoomed-out view of the entire buffer (not just the visible viewport), with a viewport-indicator rectangle the user can click/drag to scroll.

**Read order for cold pickup:**
1. This PRD (you are here).
2. `CLAUDE.md` § "Source layout" + gotchas #10, #22, #23 (#11 is retired — see the migration banner above).
3. The phase you are about to work on (skip the others on first pass).
4. Background research links in §10 if you need to re-validate technical choices.

---

## 1. Vision

The editor pane grows a narrow minimap column on its right edge that renders the full buffer contents — not the viewport, the entire file — at a heavily zoomed-out scale. Users can see document shape (indent structure, diagnostic clusters, search-hit density) at a glance and click anywhere on the minimap to jump there. The viewport-indicator rectangle on the minimap is the analog scrubber; dragging it scrolls the buffer.

The two reference implementations are VS Code's `MinimapCharRenderer` (pre-baked 95-glyph sprite atlas, blitted per-cell at 1×2 px or 2×4 px) and Zed's minimap (a second `Editor` instance rendering through the same GPU glyph atlas at `font_size = 2`). Both rely on **the invariant that the full document is already tokenized**. Our embedding model breaks that invariant — NeoVim's `ext_linegrid` emits highlights only for the visible viewport — so a real chunk of the work here is building an off-viewport content + highlight pipeline that doesn't exist anywhere in `src/` today.

**Non-goals of this PRD:**
- Multi-cursor or text-selection visualization on the minimap (parked — comes after the structural surface ships).
- Animated transitions for minimap scroll (cf. the editor's spring system in `nvim_view.py` gotcha #11) — the minimap snaps; it does not spring.
- A minimap for the terminal pane or the agent pane. Editor only.
- Configurable minimap width / position. Fixed right-edge column, fixed width, theme-driven. Configuration is a follow-up.
- Per-buffer disable. Either on for the editor or off globally.

---

## 2. Phase sequencing

Phases are ordered by dependency, not by user-facing visibility. The first three are mostly invisible plumbing; the user-facing payoff lands in Phase 2 (block-mode minimap is already useful) and is sharpened in Phase 5 (glyphs).

| # | Phase | User-facing? | Estimated size | Blocks |
|---|-------|--------------|----------------|--------|
| 0 | Surface skeleton (empty `MinimapView` + QML slot + scroll wiring) | No — empty rectangle | Small | All later phases |
| 1 | Full-buffer content channel (Lua → Python RPC + ingest) | No — debug log shows lines | Medium | 2, 3, 5, 6 |
| 2 | Block-mode rendering (per-line solid colors, indent + diagnostic gutter) | Yes — visible minimap | Medium | 4 |
| 3 | Viewport indicator + click-to-scroll | Yes — interactive scrubber | Small | — |
| 4 | Diagnostic + git-diff overlays | Yes — minimap shows file health | Small | — |
| 5 | Glyph sprite atlas + per-cell character rendering | Yes — minimap shows real letters | Large | 6 |
| 6 | Off-viewport highlight pipeline (tree-sitter parallel parse OR extmark batch) | Yes — minimap glyphs colored | Large | — |

This is the project's standard "structural surface first, visual treatment second" cadence (per `.claude/memory/feedback/ui_surface_discipline.md` and how AgentPane shipped). Phases 0–4 produce a fully usable minimap and are the **first shipping milestone** (see §11 decision 1). Phases 5–6 follow as a separate milestone after 0–4 stabilizes.

**Phase ordering rationale.** Phase 1 (content channel) blocks everything visual because we need something to draw. Phase 2 (block-mode) is the cheapest thing we can render that's still useful — it's what VS Code calls `renderCharacters: false` mode, and a meaningful fraction of users prefer it because tiny anti-aliased letters look like mush below ~3 px. Phase 5 (glyphs) is technically separable from Phase 6 (highlights) — uncolored glyphs are legible — so we land them as distinct phases to keep each PR reviewable.

**Indefinitely deferred** (no phase number, no date):
- Search-hit overlay on minimap (depends on a search infrastructure that doesn't exist yet).
- Folds rendered as collapsed bars (folds are a Phase 2+ editor concern; gotcha #22 already calls out that wrapping/folds break the logical-vs-display row distinction).
- Multi-pane minimap (one minimap per split window). The editor doesn't expose multi-window UI today.
- Animated viewport indicator. The user-visible cue is the snap, not the slide.

---

## 3. Cross-cutting concerns

These rules apply to every phase. Future-you: read this section even if you skip phase content; skipping it has burned this codebase before.

### 3.1 Threading and signals

- The minimap content channel (Phase 1) will introduce a NEW Lua → Python `vim.rpcnotify` route alongside `"capsule"`, `"completions"`, and `"whichkey"`. Subscribe in `NvimBackend._handle_notification` exactly as the existing routes do.
- Any cross-thread signal from `NvimBackend` to `MinimapModel` / `MinimapView` uses **explicit `Qt.QueuedConnection`** at the connect site with a one-line `# queued: <reason>` comment. (Project standards §4 P2.)
- If we add a worker thread (e.g., a tree-sitter parse thread in Phase 6), it MUST be `daemon=True` AND own a `threading.Event` for cooperative shutdown. (Project standards §1 P0.) Mirror `NvimBackend` / `TerminalBackend` / `SessionHost` shape.
- GC is suspended around worker-thread signal emission whenever payload construction allocates. See `CLAUDE.md` gotcha #10 — Phase 6's parser thread will allocate AST nodes; the emit site must `gc.disable()` / `gc.enable()` around the signal.

### 3.2 Paint hot path (gotcha #10 is non-negotiable)

The `MinimapView` is a sibling `QQuickPaintedItem` to `NvimView` and runs on the same `QSGRenderThread`. Every rule that applies to `NvimView.paint` applies here:

- **Memoize every `QColor`.** Reuse the `_rgb_to_qcolor` cache in `nvim_view.py:122-148` or build a parallel one in `minimap_view.py`. Never construct a `QColor` inside `paint()`.
- **Pool every `QRectF` / `QRect`.** Reuse a small set of pool objects via `.setRect(...)`. Never allocate `QRectF(x, y, w, h)` inside `paint()`.
- **Never call `QPainter.drawText` at minimap scale.** `drawText` allocates internally and is the worst-case for the GC race. Phase 5's glyph atlas is `QPainter.drawImage(source_rect, target_point)` from a pre-rasterized `QImage` — pure blit, no allocations in the inner loop.
- The Phase 0 / Phase 2 painters draw rectangles only — they are trivially safe.

### 3.3 Design tokens

- Every pixel of new QML chrome and every color in `minimap_view.py` binds to `Theme.*` tokens from `qml/design/Theme.qml`.
- New tokens land in `Theme.qml` first with a provenance comment, then are referenced from delegates and the Python painter. The Python side may hold hex strings parallel to QML (cf. the `_ANSI_PALETTE` precedent in `terminal_view.py`); if so, add a drift-detection test in `tests/test_minimap_view.py` matching the pattern of `test_ansi_palette_matches_theme_qml`.
- Minimum new token surface (Phase 2): `Theme.color.minimap.background`, `Theme.color.minimap.indent.{1..4}`, `Theme.color.minimap.viewportFrame`, `Theme.color.minimap.viewportFill`. Phase 4 adds `Theme.color.minimap.diagnostic.{error,warn,info,hint}` + `Theme.color.minimap.gitDiff.{added,modified,deleted}`. Phase 5 adds `Theme.color.minimap.glyph.{default,comment,keyword,string,number,type,function}` (a small token-color palette, NOT one entry per tree-sitter capture — minimap glyphs at 2–4 px cannot meaningfully render 30+ distinct colors).

### 3.4 Testing

- Hermetic offscreen tests via `QT_QPA_PLATFORM=offscreen`. The session-scoped `qt_app` fixture in `tests/conftest.py` is automatic.
- Use `_FakeNvimBackend` (extend the existing pattern in `tests/`) — never spawn real NeoVim subprocesses in unit tests.
- `pytest-qt` idioms (`qtbot.waitSignal`, `qtbot.waitUntil`) — never `time.sleep` (project standards §1 P0).
- New regression tests guard the failure modes documented under each phase's "Risks." Every Risk that doesn't have a corresponding test is a future bug waiting to happen.

### 3.5 Performance budget

- Repaint cadence: on scroll, on edit, on theme change. NOT per-frame at 60 Hz. Phases 2–5 gate repaints on a `_dirty` flag set by content / scroll / theme signals; the painter is otherwise idle.
- Cache the composed minimap as a `QImage` once construction completes (Phase 5 specifically). On scroll-only updates, only the viewport indicator's rectangle changes — full minimap rebuild is wasted work. On edit, invalidate only the affected line range.
- Per-paint allocation budget: zero `QColor`, zero `QRectF`, zero `QImage` constructions inside `paint()`. Pool everything.

### 3.6 No silent feature-flag or backwards-compat debt

The user has been explicit (per `~/.claude/CLAUDE.md`): no half-finished implementations, no shims, no "just in case" code. Mark hacks with `HACK` / `WORKAROUND` per the global guideline; surface them in the response summary so the user can decide whether to accept them.

---

## 4. Phase 0 — Surface skeleton

**Goal:** Land an empty `MinimapView` (a `QQuickPaintedItem` that paints a solid `Theme.color.minimap.background` rectangle) on the right edge of the editor pane. Scroll position from `NvimView._scroll_anim` is exposed and consumed by `MinimapView` — verifiable by logging — but nothing visible is drawn from it yet.

This phase is invisible to the user except for the colored rectangle. It exists to make Phases 1–5 small and reviewable.

### 4.1 Surface changes

**New files:**
- `src/symmetria_ide/minimap_view.py` — `MinimapView(QQuickPaintedItem)`. Constructor takes no positional args (QML-instantiated via `QmlElement`). Properties: `scrollPosition: float` (display rows; bound from `NvimView` via QML or via `AppController`), `bufferRowCount: int` (total buffer lines; default 0 — Phase 1 populates). `paint()` fills `boundingRect()` with the background token; no other drawing.
- `qml/MinimapView.qml` — thin QML wrapper that instantiates the Python type, binds `scrollPosition` to `nvimView.scrollAnimationPosition` (new `@Property` on `NvimView`), binds width to a `Theme.sizing.minimap.width` token (~80 px starting value, tunable).

**Edits:**
- `qml/Main.qml:307-424` — inside the `mainContent` Item, when `editorVisible`, wrap `NvimView` in a `RowLayout` (or anchor `MinimapView` to NvimView's right edge directly) so the minimap sits flush with the editor's right border. Verify the layout doesn't break the `terminalVisible` / `agentVisible` swap chord (`Ctrl+Shift+E` — gotcha for the swap logic: minimap visibility must follow editor visibility, not swap independently).
- `src/symmetria_ide/nvim_view.py` — expose a new `@Property(float, notify=scrollPositionChanged)` returning `self._scroll_anim.position`. Emit `scrollPositionChanged` from `_on_frame_swapped` (where the scroll tick already happens). Cost: one emit per animation frame, dropped to one emit per settled state when the spring decays.
- `qml/design/Theme.qml` — add `Theme.color.minimap.background` (start with a value slightly darker than `Theme.color.editor.background` so the minimap visually separates without a hard border) and `Theme.sizing.minimap.width`.

### 4.2 Acceptance criteria

- IDE launches, editor is visible, a colored rectangle is visible on the right edge of the editor at the configured width.
- Resizing the IDE window does not break the layout; the minimap stays flush right and the NeoVim grid resizes to fill the remaining space (verify `grid_resize` events fire with the new column count).
- `python -m pytest tests/test_minimap_view.py` passes with an offscreen smoke test that constructs a `MinimapView`, paints once, and asserts it didn't raise.
- Swap chord `Ctrl+Shift+E` toggles editor ↔ terminal without minimap leaking into the terminal pane.

### 4.3 Risks

- **R0.1:** Minimap layout steals horizontal space from `NvimView`, shrinking the grid. `_h_grid_resize` already handles this, but the spring's `max_delta = slot_start` invariant (gotcha #11) depends on `slot_start = (scrollback_rows - grid.rows) // 2` — if the grid shrinks, both decrease, so the invariant holds. Test: resize-during-scroll should not corrupt scrollback (extend `tests/test_nvim_view.py` if such a test doesn't exist).
- **R0.2:** `scrollPosition` emit-per-frame inflates QML binding cost. Mitigation: keep the emit; if profiling shows it's hot, switch to emit-on-change-with-epsilon (only emit when `abs(new - last) > 0.01`).

---

## 5. Phase 1 — Full-buffer content channel

**Goal:** Push the full buffer's text content from NeoVim to Python on every change. Land the IPC channel, the Python-side model, and the wiring — but don't render anything from it yet (Phase 2 does that). Verifiable via logging or a debug-only painted text overlay.

### 5.1 Surface changes

**Lua side (`runtime/`):**
- New module `runtime/lua/orchestrator/minimap.lua`. Exports `setup()` (called from `init.lua` at the end of the existing setup chain).
- On `BufEnter`, `TextChanged`, `TextChangedI`, `BufWritePost`: schedule (via `vim.schedule` — gotcha #16: never inside prefix-wait) a snapshot emit. Snapshot shape:

  ```
  vim.rpcnotify(0, "minimap", {
    op = "snapshot" | "patch",
    bufnr = <number>,
    line_count = <number>,
    lines = { "line1", "line2", ... },  -- snapshot: full; patch: only changed range
    first = <number>,                   -- patch only: 0-indexed start row
    last = <number>,                    -- patch only: 0-indexed end-exclusive row
  })
  ```

- Debounce edit-driven snapshots at ~16 ms (one frame). `BufEnter` snapshots are immediate.
- For large buffers (>10k lines or >1MB), emit `op = "snapshot"` in chunks of 1000 lines with a final `op = "snapshot_end"` marker. Phase 1 starts with no chunking; the threshold gates the optimization. Defer the chunking implementation to Phase 1.5 if testing reveals jank on large files.

**Python side:**
- New module `src/symmetria_ide/minimap_model.py`. `MinimapModel(QObject)` holds `self._lines: list[str]` and `self._line_count: int`. `apply(payload: dict)` mutates `_lines` (`snapshot` replaces; `patch` splices). Emits `linesChanged(int first, int last)` after every apply.
- `nvim_backend.py` — subscribe to `"minimap"` notifications in `_handle_notification` alongside `"capsule"` / `"completions"` / `"whichkey"`. Forward via a new `minimap_event(dict)` Qt signal.
- `app.py` — `AppController` instantiates `MinimapModel`, connects `backend.minimap_event → model.apply` with `Qt.QueuedConnection`. The model is exposed to QML via context property `minimapModel`.

### 5.2 Subscribe race (gotcha #2 applies)

The Lua module's first emit may fire before Python subscribes. Mirror the capsule-protocol fix: after Python subscribes, `nvim.async_call` an `exec_lua("_G.symmetria_minimap_push_snapshot()")` to force a re-emit. The Lua-side helper exposes `_G.symmetria_minimap_push_snapshot` for exactly this reason.

### 5.3 Acceptance criteria

- Open a file: the Python log (or a temporary debug `print`) shows the full buffer's line count.
- Edit the file: line count updates within ~16 ms of the change; patch emits show the affected range only.
- Switch buffers: snapshot fires; the model contents reflect the new buffer.
- Tests cover snapshot + patch ingest, including edge cases (empty buffer, single-line buffer, edit at row 0, edit at last row).

### 5.4 Risks

- **R1.1:** Large-buffer snapshot stalls the GUI thread. Mitigation: chunking (deferred to 1.5) + the debounce. If `TextChangedI` fires per keystroke in a 100k-line file, the snapshot is multi-MB and msgpack-encoding dominates. Test: open a synthetic 50k-line file and verify perceived editor latency remains under ~50 ms during sustained typing.
- **R1.2:** Buffer encoding edge cases. NeoVim returns lines as bytes in some configurations; pynvim's defaults decode to UTF-8 but invalid sequences raise. Wrap the apply path in a try/except and log dropped patches rather than crashing.
- **R1.3:** Editing in insert mode emits `TextChangedI` per keystroke; the debounce + patch path must handle rapid successive emits without dropping changes. Verify the patch range stays correct under rapid edits.

---

## 6. Phase 2 — Block-mode rendering

**Goal:** Render a useful minimap from the content channel using solid color blocks per line — no character glyphs. This is VS Code's `renderCharacters: false` mode. Each minimap row is one buffer line, drawn as a horizontal bar whose color encodes indent depth (or a uniform foreground if we go that route).

Concrete visual model: for each buffer line, compute leading-whitespace count → indent level (clamped to 4 levels). Draw a horizontal bar from `x = indent_level * indent_pixel_step` to `x = right_edge` in a per-indent color. Empty lines render as a gap. The result reads as a "document silhouette" — sections, code blocks, and paragraphs are immediately visible without per-character resolution.

### 6.1 Surface changes

**`src/symmetria_ide/minimap_view.py`:**
- Replace the Phase 0 `paint()` with the block-mode renderer.
- New constants: `_MINIMAP_ROW_HEIGHT_PX = 2` (start; tunable), `_MINIMAP_INDENT_STEP_PX = 4`.
- `paint(painter)`:
  - Clip to `boundingRect()`.
  - Fill background.
  - For each `i` in `range(self._model.line_count())`: compute `y = i * row_height - scroll_offset_px`; skip if outside `boundingRect()`. Compute indent level from the line text (the model exposes `line_at(i: int) -> str`). Pick the pooled indent-color `QColor`. Fill the pooled `QRectF` at `(indent * step, y, view_width - indent * step, row_height - 1)`.
- Memoization: 5 pooled colors (background + 4 indent levels), 1 pooled `QRectF`. Mutate in place via `.setRect(...)`.
- Connect `model.linesChanged` → `self._mark_dirty` → `update()`.

**Scroll mapping.** The minimap displays the WHOLE buffer at `row_height = 2 px`. Total minimap height = `view_height` (the editor pane's full height). Buffer line `i`'s y-position is `i * (view_height / line_count)` — wait, that's incorrect: that maps the entire buffer into the view, which is the goal. So `row_height_effective = view_height / line_count` for buffers that exceed `view_height / 2 px` lines. For shorter buffers (e.g., 50-line files in a 1000-px-tall pane), `row_height = 2 px` and the minimap doesn't fill the column. Decide per-paint: `row_height = max(min_row_height, view_height / line_count)`.

### 6.2 Acceptance criteria

- Open a file with mixed indentation: the minimap shows a recognizable silhouette of the indent structure.
- Edit the file: the minimap updates within one frame after the model's `linesChanged` fires.
- Theme switch (if implemented at this point): minimap colors update without restart.
- Test: synthesize a buffer, paint to an offscreen `QImage`, sample pixels at known line positions, assert correct indent colors.

### 6.3 Risks

- **R2.1:** `paint()` becomes per-line and on large files the inner loop dominates. Mitigation: limit iteration to lines whose `y` overlaps `boundingRect()` (already in the design above). For a 50k-line file at 2 px per line, only the first ~360 lines fit in a 720 px view — but the minimap is supposed to show the WHOLE buffer, so all 50k lines are drawn. At 50k iterations per paint, the loop should still complete in <1 ms because each iteration is a pooled `setRect` + `fillRect`. Profile early.
- **R2.2:** Per-line `str.lstrip()` allocations in `paint()`. Mitigation: cache `indent_level: list[int]` in the model, recomputed on `apply()`. The painter reads cached indents, allocates nothing.

---

## 7. Phase 3 — Viewport indicator + click-to-scroll

**Goal:** Draw the viewport rectangle on the minimap (the bar showing which buffer rows are currently visible in the editor) and wire click + drag to scroll the editor.

### 7.1 Surface changes

**`src/symmetria_ide/minimap_view.py`:**
- New properties: `viewportFirstRow: int`, `viewportRowCount: int` (bound from `NvimView` via QML or `AppController`).
- `paint()` adds a final pass after block rendering: draw a translucent fill (`Theme.color.minimap.viewportFill`) over the viewport row range, and a 1-px frame (`Theme.color.minimap.viewportFrame`) at the top + bottom edges.
- Mouse handling via QML's `MouseArea` (sibling, not child — `QQuickPaintedItem` mouse events are clunky). Click → compute target row from `y` → emit `seekRow(int)` from QML to `AppController`. Drag → continuous `seekRow` emits.

**`src/symmetria_ide/app.py`:**
- New `@Slot(int) seek_to_row(row: int)` — calls `nvim.async_call(lambda: nvim.command(f"normal! {row+1}G"))`. (1-indexed for `:goto`.)

### 7.2 Acceptance criteria

- The viewport indicator is visible and tracks the editor's scroll position.
- Click on the minimap scrolls the editor to that row.
- Drag on the minimap scrolls the editor continuously (no per-event jitter).
- The indicator updates smoothly during editor scroll animation (gotcha #11's spring is the source).

### 7.3 Risks

- **R3.1:** Click-to-scroll fights the spring. If the user clicks during an in-flight scroll animation, the new target replaces the current one; verify the spring redirects rather than jumps.
- **R3.2:** Drag emits at high frequency. Throttle to ~60 Hz at the QML side (`Timer` with `interval: 16`), not per `positionChanged` event.

---

## 8. Phase 4 — Diagnostic + git-diff overlays

**Goal:** Add a 4-px gutter on the LEFT edge of the minimap showing diagnostic markers (LSP errors, warnings) as colored dots and git-diff status (added / modified / deleted) as colored bars. This is the "minimap shows file health" payoff that VS Code users rely on heavily.

### 8.1 Surface changes

**Lua side:**
- Extend `minimap.lua` to emit on `DiagnosticChanged`: `vim.diagnostic.get(bufnr)` returns the full list; we want `{ lnum, severity }` per entry. Emit as `vim.rpcnotify(0, "minimap_diagnostics", { entries = {...} })`.
- Git-diff source: extend `runtime/lua/orchestrator/git/` (or wherever git status lives — check before assuming). If git-hunk info isn't already computed, integrate with `gitsigns.nvim` if present (it exposes a Lua API for hunks per buffer) or compute via `vim.fn.system("git diff --no-color ...")` chunked from a deferred autocmd. **Open question — see §11.**

**Python side:**
- `MinimapModel` gains `diagnostics: dict[int, str]` (lnum → severity) and `git_hunks: dict[int, str]` (lnum → "added" | "modified" | "deleted").
- `MinimapView.paint()` adds a left-gutter pass: for each entry, draw a 4×row-height rectangle in the appropriate token color.

### 8.2 Acceptance criteria

- Open a file with LSP diagnostics: red/yellow markers appear in the minimap gutter at the correct rows.
- Edit a tracked file: git-diff bars appear/update.
- Hover over a minimap diagnostic marker shows a tooltip with the diagnostic message. (**Open question — see §11.**)

### 8.3 Risks

- **R4.1:** Git status emit cadence. Computing `git diff` on every `BufWritePost` is fine; doing it on every `TextChanged` is not. Cadence policy: emit on `BufWritePost`, `FocusGained`, and a debounced `TextChanged` at ~2 s.
- **R4.2:** Diagnostic clustering at minimap scale — at 2 px per row, adjacent diagnostics collapse into one indistinguishable bar. Acceptable; the visual cue is presence, not count.

---

## 9. Phase 5 — Glyph sprite atlas

**Goal:** Replace the block-mode renderer with VS Code's char-sprite-atlas approach — pre-bake a 95-glyph atlas (printable ASCII) once at startup, blit per cell during paint. Output looks like real characters at tiny scale.

### 9.1 Surface changes

**New module `src/symmetria_ide/minimap_atlas.py`:**
- Class `MinimapAtlas`. Constructor takes `(font: QFont, scale_factor: int)`. Builds a single `QImage` containing all 95 printable ASCII glyphs (32–126), each rasterized at `10×16 px` via `QPainter.drawText` into an offscreen `QImage`, then downsampled to the target cell size (`1×2 px` at 1× DPI, `2×4 px` at 2× DPI) using `QImage.scaled(..., Qt.SmoothTransformation)`. Stores the result as a single `QImage` strip of `95 * cell_width × cell_height`.
- `glyph_for(codepoint: int) -> QRect` returns the source rect inside the atlas for blitting. Codepoints outside 32–126 return the "unknown glyph" rect (a single small block).
- One atlas per `(font, scale_factor, foreground_color)`. Cache atlases in a dict keyed by tuple.

**`src/symmetria_ide/minimap_view.py`:**
- Replace the Phase 2 block painter with a glyph painter. For each visible row, for each character in the line (clipped to view width), `painter.drawImage(target_point, atlas_image, source_rect)`.
- `target_point` is a pooled `QPoint`; `source_rect` is the atlas's cached `QRect` for that codepoint.
- No `drawText` in the paint loop. No `QColor` constructions. No `QImage` constructions.

**`qml/design/Theme.qml`:**
- Add a small palette of token colors for glyphs: `Theme.color.minimap.glyph.{default, comment, keyword, string, number, type, function}` (7 entries — not 30+; minimap scale doesn't differentiate finer than this).
- Build one atlas per token color at startup, cache them on the `MinimapAtlas` registry.

### 9.2 Sub-pixel rendering technique

Direct `QPainter.drawText` at 2 px font height produces unreadable mush — Qt's font hinter degenerates below ~6 px. The mitigation, borrowed from VS Code's `MinimapCharRenderer`, is to **rasterize at high resolution and downsample with a coverage-preserving filter**. Specifically: render each glyph at 10×16 px (where the hinter still produces clean stems), then `QImage.scaled(target_w, target_h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)` to the minimap cell size. `Qt.SmoothTransformation` uses bilinear filtering, which preserves stem energy as alpha coverage — the visual result is recognizable letter shapes at 1×2 px.

### 9.3 Acceptance criteria

- IDE launches; minimap shows recognizable letter shapes (not blocks, not noise).
- Theme switch rebuilds atlases; minimap reflects new colors.
- Open a 50k-line file; paint completes in <5 ms (profile with `chrome-devtools` or `cProfile` if needed).
- Zero allocations inside `paint()` — verify with `tracemalloc` snapshot diff across paints.

### 9.4 Risks

- **R5.1:** Atlas build time at startup. 95 glyphs × 7 colors = 665 raster operations. At 10×16 px each, this should complete in <100 ms. Verify; if slower, build lazily on first paint.
- **R5.2:** Non-ASCII characters fall through to the unknown-glyph slot. Many real codebases (especially comments, strings) contain non-ASCII. Decision: Phase 5 only handles printable ASCII; non-ASCII renders as a block. A follow-up could extend the atlas to cover Latin-1 supplement or use a dynamic atlas (rasterize on first use, evict LRU). **Open question — see §11.**
- **R5.3:** Memoized atlases hold `QImage` lifetime past theme switches. Invalidate the cache on theme-change signal.

---

## 10. Phase 6 — Off-viewport highlight pipeline

**Goal:** Color the minimap glyphs by syntactic role (comment, keyword, string, etc.) rather than uniform foreground. Requires knowing the highlight for every character in the buffer — including content not currently in the viewport, which `ext_linegrid` does not provide.

This is the hardest phase. Two routes; **the user must choose before this phase starts** (see §11).

### 10.1 Route A — Parallel tree-sitter in Python

- Add `tree-sitter` and the relevant language grammars (`tree-sitter-python`, `tree-sitter-typescript`, etc.) as runtime deps.
- New module `src/symmetria_ide/minimap_highlighter.py`. Worker thread (daemon, owns shutdown event — §3.1) maintains a tree-sitter parse of the buffer. On `MinimapModel.linesChanged`, edit the tree and re-query highlights for the changed range.
- Maps tree-sitter capture names to the 7-entry token palette in §9.1.
- Emits `highlights_updated(first: int, last: int, captures: list[tuple[int, int, int, str]])` where each tuple is `(line, col_start, col_end, capture_name)`.

**Tradeoff:** Decouples us from nvim entirely — the minimap palette stays consistent regardless of which plugins decorated the buffer in nvim. But maintains a parallel highlight pipeline that can drift from the user's nvim colorscheme on edge cases (e.g., custom user-defined treesitter queries).

### 10.2 Route B — `nvim_buf_get_extmarks` batch queries

- Lua-side helper queries `vim.api.nvim_buf_get_extmarks(0, ts_namespace, 0, -1, { details = true })` after every snapshot/patch emit. Returns marks with `hl_group` fields.
- Python maps `hl_group` names to the 7-entry palette via a curated mapping (`hl_group → token`).

**Tradeoff:** Reuses the user's existing nvim highlighting — visually consistent with the editor. But coverage depends on what plugins decorated; vanilla buffers with no treesitter installed show uncolored glyphs.

### 10.3 Acceptance criteria

- Open a code file; minimap glyphs are colored according to the token palette (comments visibly different from keywords, etc.).
- Edit a function: glyph colors update within one frame after edit.
- Theme switch: glyph atlases rebuild with new colors (already in §9.3).

### 10.4 Risks

- **R6.1 (Route A):** tree-sitter parse on every keystroke. Mitigation: incremental parsing (tree-sitter's core feature) + debounce the highlight query at the same cadence as content patches.
- **R6.2 (Route B):** `nvim_buf_get_extmarks` is synchronous and blocks nvim's main loop. Mitigation: query from a `vim.schedule` callback (never from a synchronous autocmd handler), debounce.
- **R6.3 (both):** Highlight + content desync. The patch emit and the highlight emit are separate signals; the renderer must handle "content updated, highlights still arriving for the old version." Mitigation: tag every emit with a generation counter; the painter discards highlights whose generation doesn't match the current content.

---

## 11. Resolved decisions

Captured here so future sessions inherit the same constraints. All resolved 2026-05-28 by the user.

1. **Milestone target: Phases 0–4 ship as an interim milestone.** Block-mode minimap + diagnostics gutter + viewport scrubber is the first user-visible deliverable. Phases 5–6 (glyphs + highlight coloring) follow as a separate milestone after 0–4 settles. Rationale: block-mode is useful in isolation, so we get a working minimap fast; Phase 5 is the largest single phase and benefits from landing on a stable Phase 4 base.
2. **Phase 4 git-diff source: `gitsigns.nvim`.** Confirmed present in `~/.dotfiles/.config/nvim/lua/jc/plugins/gitsigns.lua`. Phase 4 reads hunks from `package.loaded.gitsigns` (its public Lua API). No shell-out to `git diff` needed.
3. **Phase 6 highlight route: Route B (`nvim_buf_get_extmarks` against the treesitter namespace).** Confirmed nvim-treesitter is present in `~/.dotfiles/.config/nvim/lua/jc/plugins/treesitter.lua`. Reusing existing highlights avoids a parallel parse pipeline and aligns minimap coloring with editor coloring by construction. Trade-off accepted: filetypes without a treesitter grammar render glyphs in default foreground.
4. **Phase 5 cell size: 2×4 px target at standard DPI (doubles to 4×8 px at 2× DPI).** Recognizable letter shapes; minimap column width budgeted at ~80 px. Closer to Zed's `font_size = 2` aesthetic than to VS Code's ultra-compact 1×2 default.
5. **Phase 4 diagnostic tooltips: deferred.** Hovering a minimap diagnostic marker does NOT show a tooltip in v1. Minimap is glanceable, not interactive; the editor surfaces the diagnostic when you scroll there.
6. **Phase 5 non-ASCII handling: block fallback for v1.** Non-ASCII codepoints render as a single solid block in the atlas's "unknown glyph" slot. Dynamic atlas / Latin-1 extension is a deferred follow-up.
7. **Phase 5 theme integration: static Symmetria-aesthetic palette.** Glyph token colors come from `Theme.color.minimap.glyph.*` and are NOT introspected from the user's nvim colorscheme. Rationale: keeps the minimap consistent with the rest of the IDE chrome; introspection adds startup cost and edge cases for marginal benefit.

---

## 12. Background research

Captured here so future sessions don't need to re-research.

**VS Code minimap technique** — pre-baked 95-glyph sprite atlas, 1×2 px or 2×4 px target cells, source rasterized at 10×16 px and downsampled with box filter. Two variants (`charDataNormal`, `charDataLight`) bake gamma-style softening for light/dark themes. Falls back to solid blocks when `editor.minimap.renderCharacters = false`.

- https://github.com/microsoft/vscode/blob/main/src/vs/editor/browser/viewParts/minimap/minimapCharRenderer.ts
- https://github.com/microsoft/vscode/blob/main/src/vs/editor/browser/viewParts/minimap/minimapCharSheet.ts
- https://github.com/microsoft/vscode/blob/main/src/vs/editor/browser/viewParts/minimap/minimapPreBaked.ts

**Zed minimap technique** — a second `Editor` instance rendering through the same GPU glyph atlas at `font_size = 2`. No bespoke sprite renderer; reuses the existing `gpui` atlas pipeline.

- https://github.com/zed-industries/zed/pull/26893
- https://zed.dev/blog/videogame (background on the gpui atlas)

**VS Code's whole-document tokenization** — tokens for the entire document are computed top-to-bottom in a single pass and stored per-line-end as state; edits only retokenize a suffix. This is the invariant that makes the minimap cheap.

- https://code.visualstudio.com/blogs/2017/02/08/syntax-highlighting-optimizations

**Why subpixel character rendering at 2–4 px needs pre-rasterized atlases.** Conventional grayscale AA at the target size produces mush because the font hinter collapses below ~6 px. Downsampling from a 10×16 raster preserves stem energy as alpha coverage. SDF is overkill at this scale.

---

## 13. Where to look first when picking up

- Phase 0: `qml/Main.qml:307-424` (where the minimap inserts) + `src/symmetria_ide/nvim_view.py:1434-1495` (where the scroll position lives).
- Phase 1: `runtime/init.lua` (where to wire the new module) + `src/symmetria_ide/nvim_backend.py` (where to subscribe).
- Phase 2: `src/symmetria_ide/terminal_view.py` (clean precedent for a small `QQuickPaintedItem` with the gotcha #10 discipline applied).
- Phase 5: `src/symmetria_ide/nvim_view.py:122-148` (`_rgb_to_qcolor` memoization pattern to mirror for the atlas registry).
- Phase 6: `runtime/lua/orchestrator/whichkey/tree.lua` (precedent for a Lua module that builds an index over buffer/global state and emits to Python).

---

## 14. Non-negotiables (project-wide)

1. **Keyboard-first.** Phase 3's click-to-scroll is a convenience, not a requirement; the editor's existing `Ctrl-d / Ctrl-u / G / gg` MUST remain the primary scroll mechanism. The minimap is a *visualization*, the scrubber is the secondary input mode.
2. **Symmetria aesthetic.** Minimap colors derive from `Theme.*` tokens, never inline literals. Minimap visual treatment is minimal — no chrome, no border, no scrollbar — just the rectangle, the content, and the viewport indicator.
3. **NeoVim motions preserved.** No minimap interaction may change buffer state in a way that breaks `u` (undo). Click-to-scroll uses `normal! NG`, which is a motion, not an edit.
4. **Compose, don't reimplement.** Use tree-sitter (Phase 6 Route A) or nvim's extmarks (Route B) for highlights; don't write a syntax highlighter from scratch.

---

## 15. Memory & doctrine cross-refs

Per `.claude/rules/memory_doctrine.md`: this PRD is an active workplan, not memory. No MEMORY.md entry. When Phase 0 ships, drop a one-line topic file under `.claude/memory/project/active/minimap_phase0_state.md` summarizing what landed and what invariants hold, so the next session can pick up the next phase without re-reading this entire PRD. When the whole minimap subsystem ships (all 6 phases or the user signals "done at Phase N"), consolidate to `.claude/memory/project/shipped/minimap.md` as a single-line pointer to `docs/minimap-prd.md` + `src/symmetria_ide/minimap_view.py`.
