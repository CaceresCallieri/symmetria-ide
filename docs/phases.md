# Phases

Each phase ends with a go/no-go checkpoint. If a phase's deliverable does not feel right, reconsider before continuing.

## Phase 0 — Spine  *(complete)*

**Goal:** one PySide6 window embeds NeoVim and renders one `orchestrator.nvim` capsule in a native QML status bar.

**Delivered:**

- `QGuiApplication` + `QQmlApplicationEngine` with `NvimView` (a `QQuickPaintedItem`) rendering the NeoVim grid at 120×30 baseline, reflowing to window size.
- `nvim --embed -n` spawned via `pynvim.attach("child")`; the user's real `init.lua` loads so motions, plugins, and colorscheme carry through unchanged. `--clean` is opt-in via `NvimBackend(clean=True)` for isolation testing.
- Our `runtime/init.lua` is injected ahead of user config via `--cmd luafile`, then re-asserts key options (`laststatus=0`, `showmode=false`) from a `VimEnter` autocmd so lualine can't clobber us.
- `Grid` (pure Python) applies redraw ops: `grid_resize`, `grid_line` (with repeat + hl_id run-coalescing), `grid_clear`, `grid_cursor_goto`, `grid_scroll`, `hl_attr_define`, `default_colors_set`, `mode_info_set`, `mode_change`, `flush`. Unit-tested in isolation from Qt (21 tests).
- Key translator (`src/symmetria_ide/keys.py`) covers special keys, modifier combos, `<LT>` escape, Ctrl-letter round-tripping from Qt's raw control codepoints.
- Capsule pipeline: Lua `rpcnotify(0, "capsule", {...})` → pynvim `notification_cb` → `NvimBackend.capsule_updated` signal → `AppController._route_capsule` → `StatusBarState.apply` (well-known ids: `mode`, `file`, `branch`, `project`, `pos`) or `CapsuleModel.update` (unknown ids).
- Native QML status bar: color-coded mode badge, project name, branch (read directly from `.git/HEAD`, no subprocess), file path with middle-elide, cursor position with percent, `symmetria` brand tag. Lualine hidden from the editor viewport.

**Status-line extraction (pulled forward from Phase 3):** what was originally a Phase 3 deliverable — replacing lualine with a native QML bar — happened during Phase 0 because the capsule pipeline already existed. Only the command-line and message extraction (`ext_cmdline`, `ext_messages`) remain for Phase 3.

**Checkpoint cleared:** feel, aesthetic, and parity with stock NeoVim all hold. Continuing.

## Phase 1 — File Manager integration  *(deferred — needs scope decision)*

**Original goal:** the Symmetria File Manager runs as a panel inside the IDE window and opens at the focused NeoVim project root.

**Blocker uncovered in Phase 0:** The existing Symmetria File Manager is a QuickShell application, not a plain Qt/QML one. Its UI layer depends on QuickShell-specific types (`FloatingWindow`, `Config`/`Theme`/`Logger` singletons, systemd-service-with-IPC model). Only the C++ plugin (`Symmetria.FileManager.Models` — installed at `/usr/lib/qt6/qml/Symmetria/FileManager/Models/` providing `FileSystemModel`, `FuzzyFinder`, preview helpers) is dependency-free.

**Three paths to decide between:**

1. **Full rewrite of `symmetria-file-manager` from QuickShell to plain Qt.** Aligns with the user's stated "QuickShell was a mistake for the File Manager" position. Multi-day effort; touches a repo used daily; breaking change for the standalone app. Highest long-term value.
2. **Port-only:** copy the minimum QML file-list / fuzzy-finder components into `symmetria-ide/qml/`, consume the existing C++ plugin directly. No touch to the standalone file manager. Short-term win, eventual duplication.
3. **Skip for now; revisit after Phase 2.** File Manager integration has no runtime dependency from Phase 2 (agent pane) or Phase 3 (command-line extraction). The agent pane is the biggest-payoff phase per this doc and should not block on File Manager decisions.

**Current choice:** path 3. Phase 1 is deferred; next work is Phase 2.

**Checkpoint (unchanged, for when we return):** can the full File Manager workflow (including fuzzy search) happen inside the IDE window with no regression?

## Phase 2 — Claude Code frontend  *(next, biggest payoff)*

**Goal:** Claude Code runs inside the IDE's agent pane with native rendering for images, HTML diagrams, and conversation blocks.

**Deliverables:**

- pty spawn (`ptyprocess`) + `pyte` cell model.
- QML terminal view rendering pyte state.
- Warp-style block model — each prompt/response is a navigable, selectable block.
- Inline image rendering when Claude Code references an image path.
- Inline HTML diagram rendering via embedded `QtWebEngineView` (no external browser tab).
- Keyboard navigation across history blocks.

**Checkpoint:** can a full Claude Code session happen here with better observability than the terminal?

## Phase 3 — NeoVim chrome extraction

**Goal:** extract NeoVim's native command line and messages into native QML.

**Already done in Phase 0** (scope reduction):

- Status line replacement (lualine hidden, native QML bar with mode badge, branch, file, position).

**Still on the list:**

- `ui_attach` with `ext_cmdline`, `ext_messages` enabled.
- Native QML command palette renders NeoVim's `:` state.
- Messages render in a native toast / log panel.

**Checkpoint:** does motion feel preserved? If anything feels laggier than stock NeoVim, fix before continuing.

## Phase 4 — Agentic browser

**Goal:** an embedded browser the agent can drive, avoiding the Hyprland workspace escape problem.

**Deliverables:**

- `QtWebEngineView` pane.
- cmux-inspired control surface: agent sends `navigate`, `click`, `fill`, `eval_js`, `snapshot_accessibility_tree`.
- Agent pane and browser pane share state — screenshots flow into agent history automatically.

**Checkpoint:** can an agent complete a browser task end-to-end without spawning an external browser?

## Far future

- **Own window manager** — fork Hyprland (~2 years out, dependent on LLM advancement). May reopen the federation architecture question.
- **gpui migration** — rewrite in Rust once gpui is stable, informed by everything learned in Phases 0–4.
- **Own editor core** — replace NeoVim's editing buffer itself. Only if the gpui migration motivates it.
- **Custom agent harness** — Phase 2's frontend is agent-agnostic at the IPC layer. Any prompt/response protocol (OpenCode, PyAgent, custom) can plug in.
