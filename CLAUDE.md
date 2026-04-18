# Symmetria IDE

A custom IDE wrapper built on NeoVim, in the Symmetria ecosystem.

**Phase 0 spine complete. Phase 1 deferred. Phase 2 (Claude Code agent pane) is next.**

## Status at a glance

- **Framework:** PySide6 (Qt 6 + Python + QML), migrating to gpui/Rust long-term.
- **Core embed:** NeoVim via `--embed` + msgpack-RPC through `pynvim`. User's real `~/.config/nvim` loads by default (plugins, colorscheme, keymaps all work).
- **Status bar:** native QML with mode badge, project, branch, file path, cursor position. Lualine is hidden from the viewport (laststatus=0 re-asserted on VimEnter).
- **Constraints:** keyboard-first, Symmetria aesthetic, NeoVim motions preserved.
- **Runtime deps on Arch:** `sudo pacman -S --needed pyside6 python-pynvim`.
- **Run:** `PYTHONPATH=src python -m symmetria_ide`.
- **Dev workflow doc:** `docs/dev-workflow.md` — env vars for headless smoke testing, Hyprland workspace-6 rule, notification-system quirks.

## Source layout

- `src/symmetria_ide/grid.py` — pure-Python Grid state (applies `grid_line`, `grid_scroll`, etc.).
- `src/symmetria_ide/nvim_backend.py` — pynvim worker thread; RPC from GUI thread must be marshaled via `nvim.async_call` (see gotcha #1 below).
- `src/symmetria_ide/nvim_view.py` — `QQuickPaintedItem` rendering the grid; coalesces runs of same-highlight cells into single `fillRect` + `drawText` calls.
- `src/symmetria_ide/keys.py` — Qt key event → NeoVim keycode translator (unit-tested).
- `src/symmetria_ide/app.py` — `QGuiApplication`, `StatusBarState` (well-known capsules with per-field notify signals), `CapsuleModel` (generic extension slot), `AppController`.
- `qml/Main.qml`, `qml/StatusBar.qml` — UI.
- `runtime/init.lua` — status-line replacement + capsule emitter. Will be replaced by real `orchestrator.nvim` later; the protocol (below) is the contract.

## The capsule protocol

Lua emits `vim.rpcnotify(0, "capsule", { id = "...", label = "...", value = "..." })`. Python routes by id:

| id        | surfaced in                   | emitted on                                |
|-----------|-------------------------------|-------------------------------------------|
| `mode`    | `StatusBarState.mode`         | ModeChanged, VimEnter                     |
| `file`    | `StatusBarState.file`         | BufEnter, BufWritePost                    |
| `branch`  | `StatusBarState.branch`       | BufEnter, DirChanged                      |
| `project` | `StatusBarState.project`      | BufEnter, DirChanged                      |
| `pos`     | `StatusBarState.position`     | CursorMoved, CursorMovedI                 |
| anything else | `CapsuleModel` (generic)  | plugin-defined                            |

## The completion pipeline (separate from capsules)

For cmdline autocomplete, we run our own `getcompletion()`-based pipeline instead of relying on `ext_popupmenu` — plugin ecosystems (`nvim-cmp` with `cmp-cmdline`, `wilder.nvim`) draw their own floating windows anchored to the *default* bottom-row cmdline position, which looks broken once we've extracted the cmdline into a floating overlay.

Pipeline shape: `CmdlineEnter`/`CmdlineChanged`/`CmdlineLeave` autocmds in `runtime/init.lua` call `vim.fn.getcompletion(line, "cmdline")` and emit `vim.rpcnotify(0, "completions", { items, line, selected })`. Python subscribes to `"completions"` alongside `"capsule"` and routes payloads to `CompletionModel` (a `QAbstractListModel` with a single `word` role and a `selected` property). `CommandLine.qml` binds its popup exclusively to `completionModel`, so the popup is independent of whatever the user has installed.

Tab navigation works via a `c`-mode keymap installed at each `CmdlineEnter` (scheduled via `vim.schedule` so it wins over plugin keymaps that might also install during `CmdlineEnter`). The keymap calls a Lua `cycle_completion(direction)` that advances through the cached list and calls `setcmdline()` on the chosen item. `emit_completions` then fires from the resulting `CmdlineChanged`, but uses **equality-based cycle detection** (does the live cmdline text match any cached item?) to keep the list stable and emit the matching row as `selected` — this is robust regardless of whether `CmdlineChanged` fires synchronously or deferred.

We also force-disable `nvim-cmp`'s cmdline source from our `VimEnter` handler (`cmp.setup.cmdline(":", { enabled = false })`, plus `/` and `?`) so its floating popup doesn't render at the default bottom-row cmdline position. `noice.nvim` users still need to add their own `vim.g.symmetria_ide` guard since we don't override noice post-setup.

To add a new well-known capsule, add the field + notify signal to `StatusBarState` and bind it in `StatusBar.qml`. To add a plugin-defined one, just emit it from Lua — it falls through to `CapsuleModel` and a future delegate can render it.

## Non-obvious gotchas (burned in Phase 0, don't relearn)

1. **pynvim is not thread-safe.** Any RPC call from the Qt GUI thread raises `NvimError: request from non-main thread`. Always marshal through `nvim.async_call`.
2. **Subscribe race.** `init.lua` runs during nvim startup and fires its initial capsule push *before* Python subscribes to the `"capsule"` notification. Python must actively re-request the push (via `exec_lua("_G.symmetria_push_state()")`) after subscribing, or the status bar stays empty until the first mode change.
3. **QML non-bindable function calls don't re-evaluate.** `Text.text: root.capsules.rowCount()` computes once and stays stale. Use role data inside a `Repeater` delegate, or bind to a property with a notify signal. This is why well-known capsules live on `StatusBarState` (per-field `@Property` with `notify=`) rather than in `CapsuleModel`.
4. **Window app_id** is set via `QGuiApplication.setDesktopFileName("symmetria-ide")` — this becomes the Hyprland window class, so `windowrule = workspace 6 silent, class:^(symmetria-ide)$` matches.
5. **Lualine will clobber `laststatus`.** Plugins set their own status-line config during `setup()`. Setting `laststatus=0` via `--cmd` is NOT enough — plugin setup runs after and wins. Re-set from a `VimEnter` autocmd to have the last word.
6. **Notification daemon on this system is Symmetria Shell (QuickShell), not swaync/mako.** `swaync-client` and `makoctl` are no-ops here.
7. **Pyright noise is not a bug.** PySide6 stubs mis-type `QAbstractItemModel.data`/`rowCount`/`roleNames` parameters, flag `@property` + `@QmlElement` decorators, and can't resolve relative imports without a `pyrightconfig`. Runtime works. Don't try to "fix" these by changing signatures to match the stubs — that breaks Qt's metaobject system.
8. **Plugins that also claim `ext_cmdline`/`ext_popupmenu` will conflict.** `noice.nvim` is the common case: it tries to draw its own cmdline overlay, notices an external GUI already claimed the extension, and echoes a warning into the grid every time the cmdline opens. `runtime/init.lua` sets `vim.g.symmetria_ide = 1` as a detection flag so the user's config can disable noice's cmdline/popupmenu modules conditionally (pattern borrowed from `g:goneovim`). Same applies to any future plugin consuming these ext flags.
9. **`cmdline_show` arg count varies by NeoVim version.** 0.9 sends 6 positional args (`content, pos, firstc, prompt, indent, level`); 0.10+ sends 7 (adds `hl_id` for firstchar). The backend handler accepts `_hl_id` with a default. Same forward-compat pattern (`*_rest: Any`) absorbs future additions on `cmdline_pos`, `cmdline_hide`, `popupmenu_show` without crashing.
10. **Python 3.14 cyclic GC will SEGV us if the paint hot path allocates.** 3.14 tracks even tuples of primitives (regression vs 3.13) and runs incremental collections more aggressively. When the Qt `QSGRenderThread` is mid-`paint()` — specifically inside a PySide6 → Qt C++ call like `painter.setPen(...)` — it's holding C++ pointers to Python-owned wrappers with the GIL released. If the pynvim worker thread simultaneously trips `gc.threshold` and runs a collection, the render thread's C++ access SEGVs. Two mitigations stack:
    - **Suspend GC during `_dispatch_redraw`** in `nvim_backend.py` (the allocation-heaviest worker code path: `apply_line` creates a `Cell` per updated grid position).
    - **Freeze long-lived state after startup** via `gc.freeze()` just before `app.exec()` inside `app.run()`, excluding Qt wrappers / QML engine / controller from every future collection.
    - **Memoize every QColor** in `_rgb_to_qcolor` (`nvim_view.py`). This was the decisive fix — `_paint_row` previously allocated two fresh QColor shiboken wrappers per highlight run, hundreds per frame. Caching collapses the working set to one entry per distinct RGB value used by the colorscheme. General rule for paint code: **any PySide6/shiboken wrapper allocated inside `paint()` is a GC/race hazard** — cache it, pool it, or mutate in place. `QRectF` is the next likely candidate if this class of crash ever returns. `faulthandler.enable` is armed at import time in `__main__.py` and writes to `$XDG_STATE_HOME/symmetria-ide/crash.log`, so any relapse will leave a trace with the exact frame to look at.
11. **Smooth-scroll geometry invariants** (all in `nvim_view.py`). The scroll animation is a critically-damped spring over a 2×+ scrollback buffer. Several subtle bugs hid in this code; the rules below are derived from real visible regressions the user hit. Violate them and you will reintroduce specific breakage.
    - **`max_delta` for the spring clamp is `slot_start`, NOT `scrollback_rows - grid.rows`.** The headroom actually available to `paint()` above and below the center slot is `slot_start = (scrollback_rows - grid.rows) // 2` — only ONE side. Using the full `scrollback_rows - grid.rows` allows `position` to drift to 2× what `paint()` can render, so the far side of compound scrolls reads past the buffer edges and shows a blank band of `default_bg`. Reintroducing this bug manifests as "big portions of lines disappearing before they leave the viewport" on rapid Ctrl-d/Ctrl-u.
    - **`SCROLLBACK_MULTIPLIER` must be at least 3 for half-page scrolls to compound.** With `mult=2`, `slot_start = rows/2` — a single half-page Ctrl-d consumes the entire headroom and a second one in quick succession blows past the clamp (far-jump) or exposes the leak above.
    - **The paint loop must NOT iterate `dr = grid.rows` when `pixel_residual_y >= 0`.** The trailing extra row is only visible during sub-cell animation (residual < 0). At settled state it should sit exactly at `y = grid.rows * ch` — but QML float sizing makes `boundingRect()` marginally larger than the grid, so that row leaks into the viewport as STALE content from old scrollback rotations (user-visible as "the bottom line shows the wrong line number — 15 instead of 35, sometimes 10"). Gate the iteration on residual. `dr = -1` (leading extra row) is ALWAYS above the viewport and should simply never be iterated.
    - **Clip to exact grid dimensions, not `boundingRect()`.** `painter.setClipRect(QRectF(0, 0, cols*cw, rows*ch))` is defense in depth — even if the iteration guard above is ever loosened, the clip prevents stale scrollback content from reaching pixels outside the intended grid.
    - **Cursor TARGET uses `(cur_row - scroll_anim.position) * ch` and is retargeted every frame.** This is Neovide's `update_cursor_destination` formula (`src/renderer/cursor_renderer/mod.rs:294-330`): the cursor's pixel-space destination is the scroll-adjusted viewport row, and the target moves as the scroll spring decays. The cursor spring chases this moving target at ~90ms while the scroll spring decays at ~300ms — because the cursor spring is ~3.3× faster, the cursor visibly converges on its final row before the background finishes sliding. This produces the layered "two cooperating springs" feel where cursor and scroll have different cadences. **Historical note:** this formula was previously rejected as "the bright reverse-block lands at `(cur_row + |delta|) * ch` for the entire animation, reading as 'last line is delayed'" — that was correct at the time because there was no `CursorAnimation` spring, so the cursor just teleported to the offset row and sat there. With the cursor spring now in place (`_cursor_anim`), the spring animates AROUND the moving target, so the old failure mode is gone. DO NOT revert to `dest_y = cur_row * ch` "for simplicity" — that welds cursor to scroll as one rigid slab and kills the cadence effect.
    - **Per-frame retarget order in `_on_frame_swapped`: `scroll_anim.tick()` → `_update_cursor_destination()` → `cursor_anim.tick()`.** The scroll must tick first to produce the new `position` for this frame; we then recompute the cursor's destination from that fresh position; only then does the cursor tick advance. Swapping this order one step would make the cursor target lag one frame behind the scroll — a subtle drift that wouldn't be visible as broken motion but would dampen the cadence effect.
12. **Cursor animation spring stores the REMAINING DELTA, not the absolute position** (`CursorAnimation` in `nvim_view.py`). This is the same algorithm as `ScrollAnimation` but with a crucial twist ported from Neovide's `cursor_renderer/mod.rs`: on `set_destination`, we seed `_position_x = dest - current_painted_position` — the spring then decays that delta to 0, and `current = destination - position` inverts it back to pixel space. DO NOT "fix" this by having it store absolute position like the scroll spring does: the redirect-mid-flight semantics rely on the delta seeding, and you'd lose velocity continuity on chained cursor moves (typing a word would feel like stutter-steps instead of a single glide). The short-jump speedup (≤2 cells horizontal AND 0 vertical → `CURSOR_SHORT_ANIMATION_LENGTH=0.048s`) is picked at `set_destination` time based on the delta, not applied globally.
13. **Cursor blink uses a LINEAR wall-clock ramp, not a spring** (`CursorBlink` in `nvim_view.py`). Sampled from `time.perf_counter()` inside `_paint_cursor`, NOT accumulated per frame from `dt`. Ported from Neovide's `cursor_renderer/blink.rs`: accumulating per-frame `dt` stair-steps the opacity when the frame clock stalls (tab-away/tab-back, compositor hiccup). Any of `blinkwait/blinkon/blinkoff == 0` disables blinking (`:h guicursor` semantics). Blink phase resets on mode *shape* change only — NOT on cursor move — so typing `hjkl` rapidly does not restart the blink timer. This is the "GUI editor" feel (Neovide-matching), opposite of the "terminal" feel that resets blink on every keystroke. If you "fix" this by restarting the blink phase on every flush, the cursor will never reach its OFF phase during active editing, defeating the blink entirely.
14. **Frame driver gates on ALL animation sources.** `_animation_is_active()` must return True for scroll spring OR cursor spring OR blink; disconnecting `frameSwapped` when only one is "done" while another is still active freezes that animation at its current frame. Easy regression target when adding a 4th animation source: remember to OR it into `_animation_is_active`.

## Running tests

```
PYTHONPATH=src python -m pytest tests/ -v
```

## Phase 2 starting points

When picking up Phase 2 (Claude Code agent pane):

- Terminal deps to add: `ptyprocess` (installed system-wide via `python-ptyprocess` on Arch), `pyte` (not yet installed — `pip install pyte` or check for an Arch package).
- Reference pattern: `src/symmetria_ide/nvim_backend.py` shows the "worker thread + Qt signal" shape that the pty/pyte bridge should also follow.
- The agent pane is a sibling of the editor in `Main.qml` — add a new `AgentPane.qml` and wire a key binding at the window root to toggle focus.
- Warp's block model is the reference: each prompt+response pair is a navigable block with selectable content. `pyte.Screen` gives us cell output; we group into blocks by watching for shell prompt markers.
- Keep the IPC layer agent-agnostic (per `docs/future.md`): the frontend speaks prompt/response over pty/stdio, so OpenCode/PyAgent/custom harnesses can slot in later.

## Where to look first

- `docs/vision.md` — what we're building and why
- `docs/identity.md` — naming, tagline, design principles
- `docs/architecture.md` — embedding model, extraction strategy
- `docs/tech-stack.md` — framework decision with reasoning
- `docs/phases.md` — the phased build plan
- `docs/references.md` — Zed, Warp, cmux, Neovide
- `docs/future.md` — long-horizon direction (own WM, gpui rewrite)

## Non-negotiables

1. **Keyboard-first** — no mouse-required interactions.
2. **Symmetria aesthetic** — minimal, calm, consistent with Shell & File Manager.
3. **NeoVim motions preserved** — navigation feel is sacred.
4. **Compose, don't reimplement** — orchestrate existing tools (NeoVim, Qt, Claude Code) rather than replace them.

## Related projects in the Symmetria ecosystem

- Symmetria Shell (QuickShell-based desktop shell)
- Symmetria File Manager (QML, to be integrated in Phase 1)
- Symmetria WhatsApp (standalone, not integrated)
- `orchestrator.nvim` (NeoVim plugin driving the Claude Code workflow)
