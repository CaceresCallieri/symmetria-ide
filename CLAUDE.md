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

To add a new well-known capsule, add the field + notify signal to `StatusBarState` and bind it in `StatusBar.qml`. To add a plugin-defined one, just emit it from Lua — it falls through to `CapsuleModel` and a future delegate can render it.

## Non-obvious gotchas (burned in Phase 0, don't relearn)

1. **pynvim is not thread-safe.** Any RPC call from the Qt GUI thread raises `NvimError: request from non-main thread`. Always marshal through `nvim.async_call`.
2. **Subscribe race.** `init.lua` runs during nvim startup and fires its initial capsule push *before* Python subscribes to the `"capsule"` notification. Python must actively re-request the push (via `exec_lua("_G.symmetria_push_state()")`) after subscribing, or the status bar stays empty until the first mode change.
3. **QML non-bindable function calls don't re-evaluate.** `Text.text: root.capsules.rowCount()` computes once and stays stale. Use role data inside a `Repeater` delegate, or bind to a property with a notify signal. This is why well-known capsules live on `StatusBarState` (per-field `@Property` with `notify=`) rather than in `CapsuleModel`.
4. **Window app_id** is set via `QGuiApplication.setDesktopFileName("symmetria-ide")` — this becomes the Hyprland window class, so `windowrule = workspace 6 silent, class:^(symmetria-ide)$` matches.
5. **Lualine will clobber `laststatus`.** Plugins set their own status-line config during `setup()`. Setting `laststatus=0` via `--cmd` is NOT enough — plugin setup runs after and wins. Re-set from a `VimEnter` autocmd to have the last word.
6. **Notification daemon on this system is Symmetria Shell (QuickShell), not swaync/mako.** `swaync-client` and `makoctl` are no-ops here.
7. **Pyright noise is not a bug.** PySide6 stubs mis-type `QAbstractItemModel.data`/`rowCount`/`roleNames` parameters, flag `@property` + `@QmlElement` decorators, and can't resolve relative imports without a `pyrightconfig`. Runtime works. Don't try to "fix" these by changing signatures to match the stubs — that breaks Qt's metaobject system.

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
