# Symmetria IDE

A custom IDE wrapper built on NeoVim, in the Symmetria ecosystem.

**Phase 0 spine complete.** Next up: **Phase 1** — File Manager integration.

## Status at a glance

- **Framework:** PySide6 (Qt 6 + Python + QML), migrating to gpui/Rust long-term.
- **Core embed:** NeoVim via `--embed` + msgpack-RPC through `pynvim`.
- **Constraints:** keyboard-first, Symmetria aesthetic, NeoVim motions preserved.
- **Runtime deps on Arch:** `sudo pacman -S --needed pyside6 python-pynvim`.
- **Run:** `PYTHONPATH=src python -m symmetria_ide`.
- **Dev-only headless smoke test:** `SYMMETRIA_IDE_SCREENSHOT=/tmp/x.png SYMMETRIA_IDE_TEST_KEYS='ihi<Esc>' python -m symmetria_ide` — warms up, injects keys, grabs the Qt window from the scene graph, exits. Avoids compositor screen-capture perms entirely.

## Source layout

- `src/symmetria_ide/grid.py` — pure-Python Grid state (applies `grid_line`, `grid_scroll`, etc.).
- `src/symmetria_ide/nvim_backend.py` — pynvim worker thread; RPC from GUI thread must be marshaled via `nvim.async_call` (see gotcha #1 below).
- `src/symmetria_ide/nvim_view.py` — `QQuickPaintedItem` rendering the grid; coalesces runs of same-highlight cells into single `fillRect` + `drawText` calls.
- `src/symmetria_ide/keys.py` — Qt key event → NeoVim keycode translator (unit-tested).
- `src/symmetria_ide/app.py` — `QGuiApplication`, `CapsuleModel`, controller.
- `qml/Main.qml`, `qml/StatusBar.qml` — UI.
- `runtime/init.lua` — capsule-emission stub, replaced by real `orchestrator.nvim` later. Registers `_G.symmetria_push_state` so Python can trigger a re-push after subscribing (see gotcha #2).

## Non-obvious gotchas (burned in Phase 0, don't relearn)

1. **pynvim is not thread-safe.** Any RPC call from the Qt GUI thread raises `NvimError: request from non-main thread`. Always marshal through `nvim.async_call`.
2. **Subscribe race.** `init.lua` runs during nvim startup and fires its initial capsule push *before* Python subscribes to the `"capsule"` notification. Python must actively re-request the push (via `exec_lua("_G.symmetria_push_state()")`) after subscribing, or the status bar stays empty until the first mode change.
3. **QML non-bindable function calls don't re-evaluate.** `Text.text: root.capsules.rowCount()` computes once and stays stale. Use role data inside a `Repeater` delegate, or bind to an observable property.
4. **Window app_id** is set via `QGuiApplication.setDesktopFileName("symmetria-ide")` — this becomes the Hyprland window class, so `windowrule = workspace 6 silent, class:^(symmetria-ide)$` matches.

## Running tests

```
PYTHONPATH=src python -m pytest tests/ -v
```

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
