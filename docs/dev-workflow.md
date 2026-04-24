# Dev workflow

Concrete commands and patterns for iterating on Symmetria IDE, especially for autonomous/agent work where you can't interact with the window directly.

## Running the app

```
cd ~/projects/symmetria-ide && PYTHONPATH=src python -m symmetria_ide
```

Runtime deps (on Arch): `sudo pacman -S --needed pyside6 python-pynvim`. The app picks up the user's real `~/.config/nvim` config by default — plugins, colorscheme, keymaps all load. Pass `clean=True` to `NvimBackend(...)` to bypass user config for isolation testing.

## Agent-friendly smoke testing

The app supports a headless-ish test mode driven by env vars. It bypasses the compositor's screen-capture permissions entirely by grabbing from Qt's scene graph.

```
SYMMETRIA_IDE_SCREENSHOT=/tmp/out.png            # save PNG and exit
SYMMETRIA_IDE_TEST_KEYS="iHello<Esc>:w<CR>"       # inject NeoVim-notation keystrokes before screenshot
SYMMETRIA_IDE_AGENT_PROMPT="what is 2+2?"         # spawn claude -p with this prompt; agent pane populates
SYMMETRIA_IDE_WARMUP_MS=1500                      # ms to wait after app launches before sending keys
SYMMETRIA_IDE_SETTLE_MS=800                       # ms between key injection and screenshot
PYTHONPATH=src python -m symmetria_ide
```

Key-notation examples: `i`, `<Esc>`, `<CR>`, `:e file.txt<CR>`, `100G`, `<C-w>v`. Same syntax as nvim itself (see `:help key-notation`).

Use this pattern when you need to verify a UI change without opening a window the user can see. Screenshots land cleanly even if workspace 6 isn't active.

## Phase 2 placeholder spike

The agent pane is editor-first by design: `SYMMETRIA_IDE_AGENT_PROMPT` unset means no `claude` subprocess spawns and the pane renders its empty-state affordance ("agent pane — set SYMMETRIA_IDE_AGENT_PROMPT to populate"). Set the env var to opt in.

```
SYMMETRIA_IDE_AGENT_PROMPT="explain the capsule protocol" \
  PYTHONPATH=src python -m symmetria_ide
```

Runs the real `claude -p --output-format stream-json --include-partial-messages --verbose <prompt>` subprocess. Every JSONL event lands in `SessionModel` and renders as a flat `ListView` row. One-shot per launch — multi-turn arrives with the composer.

Prerequisite: `claude auth status` must return `loggedIn: true`. If not, run `claude auth` first; the pane surfaces auth failures via the `stderr_line` signal (logged at WARNING by `AppController._log_session_stderr`).

Combined with `SYMMETRIA_IDE_SCREENSHOT` + a longer warmup, this doubles as a headless verification of the agent pane wiring (allow 5–8 s warmup so the first streamed event arrives before the screenshot fires).

## Hyprland window routing (workspace 6)

The user's preference is that the IDE opens on workspace 6 during dev iteration, not the active workspace. `QGuiApplication.setDesktopFileName("symmetria-ide")` sets the Wayland `app_id` predictably so a window rule can match:

```
hyprctl keyword windowrulev2 "workspace 6 silent,class:^(symmetria-ide)$"
hyprctl keyword windowrulev2 "float,class:^(symmetria-ide)$"
```

Add to `~/.hyprdots/.config/hypr/` for persistence across sessions.

## Notification system

**Symmetria Shell (QuickShell-based) handles notifications on this system** — not swaync, not mako. Don't invoke `swaync-client` or `makoctl`; they do nothing. Symmetria Shell source lives under `~/.dotfiles/.config/quickshell/symmetria/`. Ask the user for the right command to dismiss notifications during testing rather than guessing.

## Running tests

```
PYTHONPATH=src python -m pytest tests/ -v
```

125 unit tests cover the pure-Python `Grid`, Qt-key translator, scroll/cursor springs, model classes (CmdlineState/CompletionModel/PopupmenuModel/WhichKeyState/WhichKeyModel), and NvimBackend shutdown paths. No Qt display needed.

## Pre-commit hooks

The project ships `.pre-commit-config.yaml` — all hooks are `language: system` and shell out to tools already installed via `paru` (`ruff`, `selene`, `stylua`, `qmllint`, `pyright`). Install once:

```
paru -S --needed python-pre-commit
pre-commit install
```

Hooks run `ruff check`, `ruff format --check`, `selene`, `stylua --check`, `qmllint`, and a report-only `pyright` pass on staged files. Wall time for a full-tree run is ≤5 s on this machine. One-off sweep against everything:

```
pre-commit run --all-files
```

Pyright currently reports ~59 known PySide6-stubs false positives (gotcha #7) and is **not** a blocking hook — the entry is wrapped in `bash -c '... || true'`. The count fluctuates as tests/classes are added; the noise pattern is `@QmlElement`/`@Property` decorators that pyright treats as returning `object`. Treat the count as an order-of-magnitude check, not a regression gate. Flip that to blocking once the baseline warning count drops to zero.

## Continuous integration

`.github/workflows/ci.yml` runs the same eight checks on every push to `main` and every pull request: `ruff check`, `ruff format --check`, `pyright` (report-only), `pyside6-qmllint`, `stylua --check`, `selene`, `pip-audit`, and `pytest` under `QT_QPA_PLATFORM=offscreen`. It runs on `ubuntu-latest` with pip-installed PySide6 — no Arch container, no Xorg/Wayland, no graphics stack. When CI disagrees with a local pre-commit run the tool versions drifted; keep them aligned by bumping both sides together.

## Inspecting what arrived over RPC

When debugging capsules or redraw events, temporarily raise logging in `nvim_backend.py`:

```python
log.debug("capsule notification: %r", payload)   # currently DEBUG
```

Change to `log.info` and run with default logging. Don't commit that — it's chatty on every cursor movement.

## Profiling suspicion

`NvimView.paint()` is the hot path — coalesces runs of same-highlight cells before calling `fillRect` + `drawText`. If redraws feel laggy, check:
1. Are we coalescing? Look at `cell.hl_id` comparisons in the `while c < grid.cols:` loop.
2. Is Qt text antialiasing thrashing? `TextAntialiasing` is currently on.
3. Is the font falling back? Preferred list is `Iosevka, JetBrains Mono, Fira Code, Cascadia Code, Source Code Pro, Hack, DejaVu Sans Mono`. Fallback is `QFontDatabase.systemFont(FixedFont)`.

## Shutdown hygiene (known nit)

When the app exits, nvim sometimes shows `process_exited return_code = -9` in stderr. The clean-shutdown handshake (`aboutToQuit` → `controller.shutdown` → `async_call("qa!")` → worker join) has a race where the Python process exits before nvim processes its quit message, so nvim dies via SIGKILL-on-parent-exit. Cosmetic — no data loss, nvim had no active buffers to save — but worth fixing eventually.

## QML notes

- `@QmlElement` registration lives in `app.py` and `nvim_view.py`. `QML_IMPORT_NAME = "Symmetria.Ide"`, `QML_IMPORT_MAJOR_VERSION = 1`.
- QML files under `qml/` are loaded via `QUrl.fromLocalFile(str(_qml_dir() / "Main.qml"))`. Hot-reload would need `pyside6_live_coding`; not wired up yet.
- Pyright warnings about `Import ".grid" could not be resolved` and `QAbstractItemModel override` signatures are PySide6 stub mismatches, not real issues. Runtime works.
