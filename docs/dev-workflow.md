# Dev workflow

Concrete commands and patterns for iterating on Symmetria IDE, especially for autonomous/agent work where you can't interact with the window directly.

## Running the app

```
cd ~/projects/symmetria-ide && PYTHONPATH=src python -m symmetria_ide
```

Runtime deps (on Arch): `sudo pacman -S --needed pyside6 python-pynvim`. The app picks up the user's real `~/.config/nvim` config by default — plugins, colorscheme, keymaps all load. Pass `clean=True` to `NvimBackend(...)` to bypass user config for isolation testing.

## Restarting the dev instance safely (NEVER pattern-kill)

**The hazard is real and live:** stable is the user's daily driver — it hosts their work across other projects AND the terminal pane this Claude session runs in. Dev and stable launch with a *byte-identical* command line (both `~/.local/bin/symmetria-ide` and `~/.local/bin/symmetria-ide-stable` end in `exec env … python -m symmetria_ide`), so `pkill -f symmetria_ide`, `pkill -f "python -m symmetria_ide"`, or `kill $(pgrep -f symmetria_ide)` **match every instance** — stable (taking down the session's own host and the user's other-project work) and every dev instance across all workspaces. This has happened; it nukes the user's entire desktop session. There is no safe command-line pattern.

**`PYTHONPATH` is the ONLY bulletproof dev/stable discriminator.** The others each fail:

| signal | stable | dev | reliable? |
|---|---|---|---|
| `PYTHONPATH` | `…/symmetria-ide-stable/src` | `src` / `…/symmetria-ide/src` (never `-stable`) | **YES** |
| `cwd` | usually `$HOME` | usually `~/projects/symmetria-ide` | **NO** — a stable instance launched from a terminal sitting in the dev dir has `cwd=repo` too (observed) |
| `SYMMETRIA_IDE_APP_ID` | `symmetria-ide-stable` | `symmetria-ide` *if set* | **NO** — leaks from the host stable IDE into every shell you spawn (`echo $SYMMETRIA_IDE_APP_ID` → `symmetria-ide-stable`), so dev launches inherit it unless you override |

So a dev launch must set `SYMMETRIA_IDE_APP_ID=symmetria-ide` explicitly (to override the leak — needed for the `match:class ^(symmetria-ide)$` workspace-6 rule and correct labelling), and any kill must gate on `PYTHONPATH` not containing `symmetria-ide-stable`.

**The safe pattern — track the PID you launched, kill only that, gated on PYTHONPATH:**

```sh
# Launch an interactive dev instance for the user to test. Set
# SYMMETRIA_IDE_APP_ID=symmetria-ide to override the leaked stable value
# (workspace 6 + correct label); record its PID:
cd ~/projects/symmetria-ide && \
  PYTHONPATH=src SYMMETRIA_IDE_APP_ID=symmetria-ide nohup python -m symmetria_ide \
  >/tmp/symmetria-ide-dev.log 2>&1 &
echo $! >/tmp/symmetria-ide-dev.pid

# Restart it later — kill ONLY the tracked PID, and ONLY if it is still a
# symmetria_ide process whose PYTHONPATH is NOT the stable worktree. This
# can never hit stable (even one launched from the dev dir) and never hits
# a non-IDE process that reused the PID:
pid=$(cat /tmp/symmetria-ide-dev.pid 2>/dev/null)
if [ -n "$pid" ] \
   && tr '\0' ' ' </proc/$pid/cmdline 2>/dev/null | grep -q 'symmetria_ide' \
   && ! tr '\0' '\n' </proc/$pid/environ 2>/dev/null | grep -qE '^PYTHONPATH=.*symmetria-ide-stable'; then
  kill "$pid"
fi
```

The two-clause verification is the interlock: clause 1 confirms the tracked PID is still a `symmetria_ide` process (guards PID reuse); clause 2 confirms it is NOT stable (the prime directive — never signal a stable instance). Note clause 2 also passes when `PYTHONPATH` is absent entirely — that is safe, because stable *always* sets `PYTHONPATH` to its worktree and clause 1 has already confirmed the PID is a `symmetria_ide` process. Do NOT substitute a `cwd` or `SYMMETRIA_IDE_APP_ID` check — both fail as shown in the table. If the pidfile is stale (user restarted manually), clause 1 fails and nothing is killed — re-launch fresh instead.

`kill "$pid"` sends SIGTERM, which triggers the IDE's graceful nvim `qa!` shutdown (shada/swap written cleanly) — so allow a moment before relaunching, or the new instance may race a still-shutting-down one. If an instance is wedged and ignores SIGTERM, escalate with `kill -9 "$pid"` **after re-running the same two-clause guard** on that PID. Never hardcode `-9` into the routine restart — it skips nvim's clean shutdown.

Note: most dev iteration doesn't need a kill at all — the screenshot harness (`SYMMETRIA_IDE_SCREENSHOT=…`) launches an ephemeral instance that exits on its own after grabbing. Reserve the launch+pidfile pattern for leaving an interactive instance running for the user.

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

Setting `SYMMETRIA_IDE_SCREENSHOT` also forces `QSG_RENDER_LOOP=basic` (single-threaded scene-graph rendering). This is load-bearing, not an optimization: the synchronous `grabWindow()` under the default threaded loop deadlocks against the GIL whenever a Python-derived `QQuickPaintedItem` (MinimapView) needs syncing, and the async `grabToImage()` alternative stalls forever on a hidden workspace. See the comments in `app.py::run` and `bootstrap.py::_grab_and_exit` before changing either side. Consequence: harness screenshots verify *content*, not threaded-render timing — threading behavior is only exercised by real launches.

## Terminal-agent runtime — manual E2E

The IDE-native orchestrator (CLAUDE.md "The terminal-agent runtime"). Full
interactive verification checklist after touching the agent pool, the
bridge client, or the chord family:

1. Launch the IDE → `Ctrl+Shift+A` → `n` → claude opens on the agent
   surface, cwd = project anchor, top-bar chip appears (sparkle + slot 1).
2. Send a prompt → the chip's sparkle animates within a hook cycle
   (activity flows `SYMMETRIA_AGENT_ID=<ide_pid>_<slot>` → hooks →
   agent-bridge → subscription snapshot → `controller.agentActivity`).
3. The Symmetria Shell dashboard shows the same agent (project, ⚠
   dangerous badge); clicking it focuses the IDE window. The default
   `✳ Claude Code` OSC title is suppressed by `_clean_agent_title` —
   chips show a title only once claude reports a real session name (`terminal_pid` = IDE pid via the declared
   `host_window_pid`).
4. `Ctrl+Shift+E` to editor → `Ctrl+1` → back on slot 1, terminal focused
   typing-ready; `Ctrl+U`/`Ctrl+D` scroll the agent's scrollback;
   `Ctrl+Shift+A` → `n` again for a second agent; `Ctrl+Shift+H/L` cycles.
5. `Ctrl+Shift+Q` closes → chip disappears, dashboard updates,
   `pgrep -f 'claude --dangerously'` shows no orphan from this IDE.
6. Quit the IDE → dashboard drops its agents (goodbye); no orphans.

Scripted spawn for headless smoke runs (composes with the screenshot
harness): `SYMMETRIA_IDE_SPAWN_AGENT=fresh` spawns one agent at launch;
an optional `:<agent-harness>` suffix selects the CLI (`fresh:opencode`).
Adding `SYMMETRIA_IDE_SPAWN_AGENT_LOCATION=vps` defers the spawn to the
VPS pairing edge and spawns it REMOTELY — the one-command E2E for the
whole location toggle (pair → toggle → sshfs mount → remote git scan →
ssh+tmux agent). Needs `~/.config/symmetria-ide/servers.json` and the
repo present under the server's repos_dir; with a long `SETTLE_MS`
(~10s) the screenshot shows the vps tab, the remote tree/branch, and
the agent pane attached to `<repo>-vps-1`. Clean up the smoke session
afterwards: `ssh dev@<host> tmux -S /home/dev/.vigilia/tmux.sock
kill-session -t <repo>-vps-1` (identity-pinned ssh, as always).
Bridge-side state is inspectable via `pkill -USR1 -f agent-bridge.py` →
`~/.local/state/symmetria/agent-bridge-diagnostic.json` (look for your
IDE pid under `clients`).

Known harness limit: the screenshot grab canvas clips chrome near the
window bottom at fractional display scale — the StatusBar (and anything
anchored there) needs an interactive launch to verify visually.

## Sidecar setup *(one-time)*

Phase 2's agent pane is driven by a Node sidecar that runs `@anthropic-ai/claude-agent-sdk` programmatically. The sidecar lives in `sidecar/` and ships built artifacts gitignored — install + build once after cloning, and again whenever `sidecar/src/**` or its dependencies change.

```
cd sidecar
npm install
npm run build
```

Requires Node `>=20` (Arch ships >=22 in the `nodejs` package). `npm install` fetches `@anthropic-ai/claude-agent-sdk` (pinned to `0.2.119`) plus dev tooling (esbuild, typescript, @types/node). `npm run build` produces `sidecar/dist/index.js`, which the Python `SessionHost` spawns at runtime. If the bundle is missing, `SessionHost.start` logs a clear error pointing right back at this section.

## Phase 2 agent pane (parked SDK chat — env-gated)

The Node-SDK AgentPane is parked AND env-gated since the terminal-agent
runtime shipped: it only mounts when `legacySdkPaneEnabled` is true —
`SYMMETRIA_IDE_SDK_PANE=1`, or implied by either env var below. The
terminal-agent runtime (previous section) is the live agent workflow.

`SYMMETRIA_IDE_AGENT_PROMPT` unset means no sidecar spawns. Set the env var to opt in.

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
hyprctl keyword windowrule "workspace 6 silent,match:class ^(symmetria-ide)$"  # Hyprland 0.56+ syntax — see CLAUDE.md gotcha #4
hyprctl keyword windowrule "float,match:class ^(symmetria-ide)$"  # Hyprland 0.56+ syntax — see CLAUDE.md gotcha #4
```

Add to `~/.dotfiles/.config/hypr/` for persistence across sessions (symlinked to `~/.config/hypr/`; `~/.hyprdots` is no longer stowed — see global CLAUDE.md dotfiles section).

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

## Shutdown hygiene

When the app exits, nvim sometimes shows `process_exited return_code = -9` in stderr. The clean-shutdown handshake (`aboutToQuit` → `controller.shutdown` → `async_call("qa!")` → worker join) has a race where the Python process exits before nvim processes its quit message, so nvim dies via SIGKILL-on-parent-exit. Cosmetic — no data loss, nvim had no active buffers to save.

**SIGTERM is now graceful** (`run()` installs `signal.signal(SIGTERM, lambda: app.quit())` + a no-op `QTimer` to wake the interpreter out of `app.exec()`). The restart recipe above sends SIGTERM, so it now runs the same `aboutToQuit → shutdown` teardown as a window close — the editor nvim gets its `qa!` and the per-instance `/tmp/symmetria-nvim-*` socket dir is `rmtree`d. Before this, SIGTERM bypassed `aboutToQuit` entirely, leaking one socket dir per restart (a long dev session had accumulated ~900). SIGINT stays `SIG_DFL` so Ctrl+C still hard-kills a wedged GUI (it does NOT clean up — use the SIGTERM path for a clean exit). As a backstop, `_reap_orphan_nvim_sockets()` sweeps dead-owner socket dirs at startup (live sockets + dirs <60s old are spared, safe under the multi-instance topology).

**The embedded nvim's cwd is guarded against `$HOME` (at the nvim seam only).** Rooting the embedded nvim's cwd at `$HOME` makes fff.nvim (user-global config) recursively `notify`-watch the whole home tree with no ignore-glob config → ~6 pinned cores at 88 °C (relay 20260614). The IDE-side guard is `_clamp_editor_root` (`app.py`): the editor pane binds `initialWorkingDirectory: controller.editorRoot` and `_sync_nvim_cwd` pushes the same clamped value, so nvim's cwd is held off `$HOME` (redirected to an inert `$XDG_STATE_HOME/symmetria-ide/scratch`) at launch AND on every cwd-sync. **The shell, file tree, and git pane deliberately stay on `displayedRoot`** — the shell MUST start at `$HOME` so `~/.dotfiles/.zshrc`'s `workspace_autocd` (gated on `$PWD == $HOME`) fires its per-workspace zoxide `cd`. An earlier fix (commit `d91539b`) instead redirected the whole *process* cwd to scratch, which silently broke `workspace_autocd` — do NOT reintroduce a process-wide `os.chdir` away from `$HOME`. The companion fix lives in `~/.dotfiles/.config/nvim/lua/jc/plugins/fff.lua` (`base_path` = git-root search with a non-`$HOME` fallback), which also protects standalone nvim.

## QML notes

- `@QmlElement` registration lives in `app.py`, `cmdline_models.py`, `whichkey_models.py`, `session_models.py`, and `minimap_view.py`. `QML_IMPORT_NAME = "Symmetria.Ide"`, `QML_IMPORT_MAJOR_VERSION = 1`.
- QML files under `qml/` are loaded via `QUrl.fromLocalFile(str(_qml_dir() / "Main.qml"))`. Hot-reload would need `pyside6_live_coding`; not wired up yet.
- Pyright warnings about relative imports not resolving and `QAbstractItemModel override` signatures are PySide6 stub mismatches, not real issues. Runtime works.

## The agentic browser (external Google Chrome)

The embedded QtWebEngine surface was retired 2026-07-27 — the browser is now a real
Chrome process the IDE spawns, pinned to the IDE's workspace by a Hyprland rule.
See CLAUDE.md "The browser panes" for the model; this section is how to exercise it.

- **The suite can never spawn Chrome or touch your compositor.** `tests/conftest.py`
  sets `SYMMETRIA_IDE_CHROME_BIN` to a nonexistent path and deletes
  `HYPRLAND_INSTANCE_SIGNATURE` (`_isolate_browser_host`), so `chrome_executable()`
  returns `""` and `hyprctl` is never shelled out to. Tests that need browser
  behaviour inject `conftest.FakeChromeHost`. Do not "fix" a test by re-enabling
  the real binary — the risk is a suite run opening windows on your screen, or
  installing a window rule that outlives it.
- **Live check without launching the IDE** — instantiate `ChromeHost` directly under
  a `QCoreApplication`, point `XDG_DATA_HOME` at a scratch dir so your real browser
  profiles are untouched, apply the pin rule by hand (a bare script has no Hyprland
  window for `ChromeHost` to resolve), then `open_window(...)` and read back
  `hyprctl clients -j`. Assert: the windows carry class `symmetria-browser-<pid>`,
  they land on the target workspace, and `hyprctl activeworkspace` is UNCHANGED —
  `silent` placement is the whole point.
- **Two behaviours are only observable live, and both were found that way:**
  Chrome always opens a window at launch, so a cold start must be pointed AT the
  requested url or you get a stray `chrome://newtab/` that CDP discovers FIRST; and
  the CDP attach always loses the race against a cold Chrome, so the client retries
  on a timer and the first window binds its target through discovery, not the
  `Target.createTarget` reply.
- **Reading the window state:** `hyprctl clients -j` filtered by class is the ground
  truth for placement; the IDE's own view is `controller._browser_tabs`. A window
  present in Hyprland but absent from the registry means adoption did not happen
  (check for a `chrome://` url — those are deliberately never adopted).

## Browser MCP server (Phase 4 Stage 2b/4 — agent control)

- **Stage 4 split (see CLAUDE.md "The browser panes"):** the IDE-side server now exposes only `browser_open` + `browser_list_windows` (visible-window allocation + url↔window correlation). Page DRIVING (navigate/eval/screenshot/network/perf/snapshot/click/fill) is delegated to off-the-shelf `chrome-devtools-mcp`, injected per-agent (needs `node`/`npx`) and pointed at the IDE-owned Chrome's CDP endpoint (`SYMMETRIA_IDE_CDP_PORT`, reserved in `app.run()` at startup and passed to Chrome as `--remote-debugging-port` when it spawns lazily).
- Needs `python-mcp` (`paru -S python-mcp`, AUR — pulls uvicorn/starlette). Optional: `AppController.start()` starts the server but failure is non-fatal, and `SYMMETRIA_IDE_BROWSER_MCP=0` disables it (manual browsing still works). Each IDE instance binds its OWN ephemeral port (the multi-instance topology rules out a fixed port) and writes a per-launch claude-shaped config to `$TMPDIR/symmetria-browser-mcp-<pid>.json` (per-agent configs add the `chrome-devtools` stdio entry alongside it); `agent_harness.spawn_argv` appends `--mcp-config <that path>` to claude agents.
- **The pieces are unit-tested but the network stack isn't in pytest** (the `QCoreApplication` fixture can't host uvicorn cleanly). End-to-end check is a standalone harness: build the app engine, `controller.start()` + `open_browser(...)`, then from a separate thread run an MCP client (`mcp.client.streamable_http.streamablehttp_client(controller._browser_mcp_server.url)`) and `call_tool("browser_list_windows", {})` — expect `{ok:true, windows:[…], focused:N}` in the result's `content[].text` (FastMCP returns the dict there, NOT always `structuredContent`). The Qt event loop MUST be running (`app.exec()` on the main thread) so the bridge's queued signals deliver. To exercise chrome-devtools driving, point `npx chrome-devtools-mcp --browserUrl http://127.0.0.1:<cdp_port>` at the same instance and `select_page` by the `browser_open`-returned url.
- Importing `uvicorn` emits 2 upstream `websockets` `DeprecationWarning`s in the suite — third-party, not a leak (`-W error::ResourceWarning` is clean).
- **Render-dependent CDP ops were a QtWebEngine limitation and should no longer bite.** `take_screenshot` and screencast used to stall on an inactive Hyprland workspace and never complete offscreen, because the embedded view's frames came from OUR throttled QtQuick render loop. Real Chrome renders itself, so the constraint moves to Chrome's own occlusion behaviour — re-measure rather than assuming either the old caveat or its absence.
