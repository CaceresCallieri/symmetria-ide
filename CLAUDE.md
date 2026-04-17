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
    - **Freeze long-lived state after startup** via `gc.freeze()` at the end of `app.run()`, excluding Qt wrappers / QML engine / controller from every future collection.
    - **Memoize every QColor** in `_rgb_to_qcolor` (`nvim_view.py`). This was the decisive fix — `_paint_row` previously allocated two fresh QColor shiboken wrappers per highlight run, hundreds per frame. Caching collapses the working set to one entry per distinct RGB value used by the colorscheme. General rule for paint code: **any PySide6/shiboken wrapper allocated inside `paint()` is a GC/race hazard** — cache it, pool it, or mutate in place. `QRectF` is the next likely candidate if this class of crash ever returns. `faulthandler.enable` is armed at import time in `__main__.py` and writes to `$XDG_STATE_HOME/symmetria-ide/crash.log`, so any relapse will leave a trace with the exact frame to look at.

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
