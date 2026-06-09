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
SYMMETRIA_IDE_AGENT_PROMPT="what is 2+2?"         # spawn the Node SDK sidecar with this prompt; the agent pane populates
SYMMETRIA_IDE_WARMUP_MS=1500                      # ms to wait after app launches before sending keys
SYMMETRIA_IDE_SETTLE_MS=800                       # ms between key injection and screenshot
PYTHONPATH=src python -m symmetria_ide
```

Key-notation examples: `i`, `<Esc>`, `<CR>`, `:e file.txt<CR>`, `100G`, `<C-w>v`. Same syntax as nvim itself (see `:help key-notation`).

Use this pattern when you need to verify a UI change without opening a window the user can see. Screenshots land cleanly even if workspace 6 isn't active.

## Sidecar setup *(one-time)*

Phase 2's agent pane is driven by a Node sidecar that runs `@anthropic-ai/claude-agent-sdk` programmatically. The sidecar lives in `sidecar/` and ships built artifacts gitignored — install + build once after cloning, and again whenever `sidecar/src/**` or its dependencies change.

```
cd sidecar
npm install
npm run build
```

Requires Node `>=20` (Arch ships >=22 in the `nodejs` package). `npm install` fetches `@anthropic-ai/claude-agent-sdk` (pinned to `0.2.119`) plus dev tooling (esbuild, typescript, @types/node). `npm run build` produces `sidecar/dist/index.js`, which the Python `SessionHost` spawns at runtime. If the bundle is missing, `SessionHost.start` logs a clear error pointing right back at this section.

## Phase 2 agent pane

The agent pane is editor-first by design: `SYMMETRIA_IDE_AGENT_PROMPT` unset means no sidecar spawns and the pane renders its empty-state affordance ("agent pane — set SYMMETRIA_IDE_AGENT_PROMPT to populate"). Set the env var to opt in.

```
SYMMETRIA_IDE_AGENT_PROMPT="explain the capsule protocol" \
  PYTHONPATH=src python -m symmetria_ide
```

Runs the Node sidecar; every SDK message translates to a JSONL event that lands in `SessionModel` and renders as a flat `ListView` row. Tool-using turns now surface an inline approve/deny card (the `permission_request` envelope, synthesized by the SDK's `canUseTool` callback) — clicking Allow or Deny writes a `permission_response` back through `SessionHost.send_permission_response` and the SDK proceeds.

Prerequisite: `claude auth status` must return `loggedIn: true` (the SDK reads the same `~/.claude` credentials as the CLI). If not, run `claude auth` first; the sidecar surfaces auth failures via the `stderr_line` signal (logged at WARNING by `AppController._log_session_stderr`).

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

The unit tests cover the model classes (CmdlineState/CompletionModel/WhichKeyState/WhichKeyModel/SessionModel/minimap), the AppController pool + central-surface + cwd-sync paths, the anchor + git controllers, the JSONL sidecar transport + session-host parser/permission paths, the `display_rows_between` scroll-unit converter, and NvimBackend shutdown. No Qt display needed.

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

The editor + shell are forked `QMLTermWidget` panes, so nvim's grid is rendered in C++ by Konsole's VT engine — there is no Python paint hot path to profile (the old `NvimView.paint()` custom grid renderer was deleted in the qmltermwidget migration). If the editor feels laggy, the suspects are:
1. The terminal transparency invariants — `useFBORendering: false` keeps rendering on the C++ image path; flipping it to the FBO path drops alpha but is no faster here.
2. Font fallback — `editor_font.py`'s `default_font()` builds the per-glyph Nerd-Font + emoji cascade once; a missing primary family forces a slower system fallback.
3. The chrome relays — heavy `vim.rpcnotify` traffic (e.g. capsules firing on every `CursorMoved`) crossing the `--listen` channel can show up as input latency rather than render lag.

## Shutdown hygiene (known nit)

When the app exits, nvim sometimes shows `process_exited return_code = -9` in stderr. The clean-shutdown handshake (`aboutToQuit` → `controller.shutdown` → `async_call("qa!")` → worker join) has a race where the Python process exits before nvim processes its quit message, so nvim dies via SIGKILL-on-parent-exit. Cosmetic — no data loss, nvim had no active buffers to save — but worth fixing eventually.

## QML notes

- `@QmlElement` registration lives in `app.py`, `cmdline_models.py`, `whichkey_models.py`, `session_models.py`, and `minimap_view.py`. `QML_IMPORT_NAME = "Symmetria.Ide"`, `QML_IMPORT_MAJOR_VERSION = 1`.
- QML files under `qml/` are loaded via `QUrl.fromLocalFile(str(_qml_dir() / "Main.qml"))`. Hot-reload would need `pyside6_live_coding`; not wired up yet.
- Pyright warnings about relative imports not resolving and `QAbstractItemModel override` signatures are PySide6 stub mismatches, not real issues. Runtime works.
