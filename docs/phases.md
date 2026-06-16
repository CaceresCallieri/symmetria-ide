# Phases

> **Framework pivot REVERSED (2026-06-07) — read first.** A Tauri 2 (Rust + React/TS) pivot was decided 2026-06-05 then reversed; the project stays on the **PySide6/QML** stack. NeoVim runs as a TUI inside a forked `QMLTermWidget` (not xterm.js, not the deleted custom grid renderer). This document is the live native roadmap; `docs/framework-pivot.md` is a superseded historical record, not the roadmap. Phases 3–4 below are native QML implementation plans. The "Delivered" sections in completed phases (0, 2, 2.5) are implementation records from the time of delivery — the pyte terminal renderer and the NvimView grid renderer they describe were both superseded by the QMLTermWidget migration; see CLAUDE.md for the live architecture.

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

**Current choice:** path 3. Phase 1 is deferred; next work is Phase 2. *(Update: path 2 partially landed outside Phase 1 scope — file-tree + git-status now reuse the FM's `Symmetria.FileManager.UI` QML module rather than the standalone FM pane. Full FM-pane embedding in the IDE remains deferred.)*

**Checkpoint (unchanged, for when we return):** can the full File Manager workflow (including fuzzy search) happen inside the IDE window with no regression?

## Phase 2 — Agent pane *(in progress — Node SDK sidecar landed)*

**Goal:** Claude Code runs inside the IDE's agent pane with structured access to every turn — images, tool calls, URLs, code blocks, **and inline approve/deny on every permission request** — instead of the terminal's byte stream.

**Architecture pivots:**

1. **`pty + pyte` → `claude -p --output-format stream-json`** (placeholder spike). Original Phase 2 used `ptyprocess` + `pyte` to spawn `claude` in a pseudoterminal and recover structure from ANSI-decorated frames. Pre-implementation discourse moved to the CLI's stream-json typed-event protocol — trivial to consume, kept every Claude Code behaviour intact. Dropped the `ptyprocess` / `pyte` optional-dep group.
2. **`claude -p` CLI scrape → Node SDK sidecar**. `-p` mode self-resolves permissions server-side and exposes no in-band approve/deny surface — any tool-using turn would either auto-deny or stall depending on `permissionMode`. Pivoted to a TypeScript Node sidecar (`sidecar/`) running `@anthropic-ai/claude-agent-sdk` programmatically, with the SDK's `canUseTool` callback as the structured permission surface. Same path Zed's `claude-code-acp`, opencode, and the official VS Code extension take.

**Delivered:**

- `sidecar/` — TypeScript Node sidecar driving `@anthropic-ai/claude-agent-sdk@0.2.119` (exact pin, no caret). Built via esbuild (`npm run build` produces `dist/index.js`); SDK marked `external` so its native binary opt-deps resolve from `node_modules` at runtime. Wire protocol in `sidecar/src/protocol.ts`; the sidecar drops `SDKUserMessage` echoes (Python's optimistic-render is the single source of truth) and translates everything else passthrough into the JSONL shapes `SessionModel` already consumes. `canUseTool` synthesizes a `permission_request` envelope on stdout and awaits a matching `permission_response` on stdin.
- `src/symmetria_ide/session_host.py` — spawns `node sidecar/dist/index.js` instead of `claude -p`. Same daemon stdout/stderr worker threads + `_stop_event` shutdown discipline. New `send_permission_response(request_id, behavior)` slot mirrors `send_user_message`; both go through a shared `_write_command` under `_stdin_lock`. Startup precondition checks `dist/index.js` exists and surfaces a clear error pointing the user at `cd sidecar && npm install && npm run build` if missing.
- `src/symmetria_ide/session_models.py` — `AgentRow` extended with `permission_state` + `request_id` fields. New `_row_from_permission_request` helper, new `resolve_permission(request_id, behavior)` slot that emits `dataChanged([PermissionStateRole])` (gotcha #3 — empty role lists force full re-bind; scoped lists let QML re-evaluate only the changed binding). New roles: `PermissionStateRole`, `RequestIdRole`.
- `src/symmetria_ide/app.py` — `AppController.respond_to_permission(request_id, decision)` slot is the QML-facing entry point; dispatches host-first (sidecar promise resolves before UI feedback), then model-resolves. The `awaitingResponse` spinner stays ON during a pending permission — only the SDK's `result` envelope flips it OFF.
- `qml/AgentPane.qml` — delegate variant for `kind === "permission_request"` rows: `Rectangle + 2 Buttons + label` card, Theme-bound, keyboard-navigable. Three states (`pending` / `approved` / `denied`) drive button-vs-status-label rendering.
- `qml/design/Theme.qml` — new `Theme.color.agent.{permissionBorder,permissionApprove,permissionDeny}` rung, aliased to mode.normal/insert/replace so the card's "awaiting / go / stop" semantics read continuous with the editor's own cues.
- Tests: 20 new across `test_session_models.py` (permission row routing, scoped `dataChanged`, idempotency), `test_app_controller_awaiting.py` (respond_to_permission ordering + spinner-stays-on), and the new `test_session_host_permission.py` (envelope shapes for both inbound commands). Replaced `test_session_host_send.py` whose envelope assertions matched the retired `claude -p` shape.

**Still to come (deferred):**

- **Permission persistence** — "always allow X for project Y". SDK exposes `PermissionResult.updatedPermissions` with `addRules` for this; surface it as a third button on the permission card.
- **Stop control** — wire the SDK's `Query.interrupt()` via a new sidecar `{type:"interrupt"}` inbound command.
- **Turn grouping + tool-call drill-in.** Flat list is the right shape for the placeholder; the SDK's richer event vocabulary now informs what the grouped view should look like.
- **Image rendering** (user-passed AND assistant-generated) and HTML/CSS diagram rendering via embedded `QtWebEngineView`.
- **URL chips + code-fence copy actions** on every delegate.
- **Focus switching** between editor and agent pane via a keyboard binding.
- **Session resume UI** — SDK exposes `resume`/`sessionId` options on `query()`.
- **MCP / hooks / slash commands via SDK options.** All available, none wired.

**Mobile / VPS outlook** (informing today's boundaries, not yet building): `SessionHost`'s interface is designed net-serialisable — dict-typed events, no Qt types at the core boundary — so a future transport (WebSocket, gRPC, Tailscale) can wrap it without rewrites. Mobile client lands as a thin remote viewer once the desktop loop is complete.

**Checkpoint:** does a full Claude Code session happen here with better observability than the terminal? — still open until the composer + permission UI land. The placeholder tells us the event vocabulary and the rendering feel are ready to design against.

## Phase 2.5 — Terminal pane + project anchor  *(all three deliverables shipped)*

**Goal:** the IDE's launch state is a terminal you navigate freely from. When you reach the project you want to work on, you anchor — file tree pins to that root, git operations target it, NeoVim becomes the focused surface for editing.

**Why this is its own phase (not folded into Phase 2 or 3):** the terminal pane is the first concrete step toward the agent-primary topology inversion described in `docs/future.md`. It's the architectural seam where "pre-anchor / post-anchor" becomes a real distinction in the codebase, and it's the foundation for shell-driven cwd integration. Folding it into Phase 2 (agent) would confuse two distinct surfaces; folding it into Phase 3 (chrome extraction) would mis-locate it as a polish layer when it's actually a topology shift.

**The three sub-deliverables** (each ships independently):

1. **Project anchor state machine *(complete)*.** `AppController` exposes `displayedRoot` (derived from `_cwd`, `_anchored`, `_anchored_root`), `anchored` (bool), and `anchor_to_current_cwd` / `anchor_to_path` / `release_anchor` Slots. File tree binds to `displayedRoot`; git controller rebinds from `cwdChanged` to `displayedRootChanged` so git operations follow the anchored root even as the raw cwd wanders. Triggers: `Ctrl+Shift+A` Qt application-scope shortcut (works from any pane, follows the IDE-level keybind precedent established here); `:SymmetriaAnchor [path]` / `:SymmetriaUnanchor` user commands as the scripted surface. New `anchor` rpcnotify channel routes Lua-emitted events to the Slots. 16 new tests (13 state-machine + 3 dispatch); 496 total in suite.
2. **Native PTY terminal pane *(shipped — 5 of 6 PRs sealed, PR 6 = paste).*** PySide6 + `pyte` HistoryScreen + `QQuickPaintedItem` (TerminalView, paint-loop discipline mirroring `nvim_view.py`). Single PTY per pane via `os.openpty()` + `subprocess.Popen([SHELL, "-l"], start_new_session=True)` so `killpg` reaps nested TUIs at shutdown. Daemon reader thread feeds bytes through pyte and emits `screen_dirty(frozenset)` cross-thread with GC-suspended emit window (gotcha #10). Renders into a Theme-tokened pane (16-slot ANSI palette aliased from `mode.*` / `text.*` where they tonally line up) in `Main.qml`'s `mainContent` Item, sibling to NvimView with mutual-exclusive `visible` bindings (gated on `controller.terminalVisible` / `controller.editorVisible`). Q2-d topology: terminal is the first-launch visible surface; Ctrl+Shift+E swaps to editor, Ctrl+Shift+T summons terminal back (both `Qt.ApplicationShortcut` chords matching the anchor precedent). Vim-motion overlay on the terminal scrollback (Warp-block-mode pattern) deferred to v2.
3. **Shell-driven cwd integration *(shipped — 3 PRs sealed).*** zsh + bash auto-injection installs the OSC 7 emitter into the user's chpwd hook (zsh) or PROMPT_COMMAND (bash) via ZDOTDIR redirect + --rcfile respectively, sourcing the user's real rcfiles first so their existing setup is preserved. Other shells log a warning and continue without sync. `TerminalBackend._parse_osc7` is a pure-function buffer-aware parser running BEFORE `stream.feed` so pyte never sees the OSC 7 bytes; handles BEL + ST terminators, percent-decoding, trailing-slash normalization, root-overwrite protection (file:/// is filtered). Extracted paths emit cross-thread via `osc7_received(str)` and route through `AppController._on_terminal_osc7` → `_route_capsule` with the synthetic `{id:"cwd", value:path}` dict — same code path as nvim's `:cd`. The anchor machinery on top works unchanged: cd while anchored updates `_cwd` silently; release re-syncs to the latest path.

**Architectural invariants:**

- Anchor is an **IDE-level concern**, not a nvim or terminal concept — triggers live as Qt application-scope shortcuts, not `<leader>` keybinds.
- `_cwd` is the **raw signal**; `displayedRoot` is a **view transformation** on top. The terminal pane will pour into `_cwd`; the anchor still pins what the UI displays. This separation is load-bearing — see the conditional `displayedRootChanged.emit()` in `_route_capsule`'s cwd branch.
- The terminal pane uses the **agent pane's "full-window swap" pattern** as its precedent — `terminalVisible` / `editorVisible` is the same shape as `agentVisible` today. Q2-d topology means the terminal is the *default* visible surface pre-anchor, swapping to nvim post-anchor (inverse of agent pane's "summoned over editor" today).

**Checkpoint:** can you launch the IDE, `cd` to a project via the terminal, anchor, and start editing — without ever touching a shell outside the IDE window? The anchor spike unlocked the state machine; the terminal pane unlocks the workflow.

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

**Pulled forward (2026-06-16); split into two stages.** The motivating pain — agent-spawned browsers escaping into Hyprland (the IDE can't track them) — was acute enough to bring this ahead of Phase 3. Decision context: the AskUserQuestion choices recorded in the plan; staged so the one real unknown (QtWebEngine on Hyprland/Wayland) is de-risked before the larger MCP plumbing.

**Goal:** an embedded browser the agent can drive, avoiding the Hyprland workspace escape problem. Containment principle: a browser escapes iff it creates a top-level OS window; embedding QtWebEngine as a QML `WebEngineView` *item* means it renders into the IDE's own window and never creates one. (On Wayland there is no reparenting/window-grab escape hatch, so embedding — or headless+screencast — is the only path.)

**Stage 1 — embedded browser surface (SHIPPED 2026-06-16):**

- A `"browser"` central surface (`qml/BrowserSurface.qml`) with embedded `WebEngineView`s, usable manually. Pool-of-views mirroring the agent pool 1:1 (fixed `Repeater` of `maxBrowserSlots` Loaders; ≤5 windows; one visible at a time; tab-chip strip; persistent `WebEngineProfile` so logins survive restart). `Ctrl+Shift+B` toggles it; the `AgentTopBar` switcher gained a `browser` entry.
- Pool state/API on `AppController` (`open_browser`/`focus_browser`/`cycle_browser_focus`/`close_browser`/`on_browser_title`/`on_browser_url`); see `src/symmetria_ide/app.py` "Embedded browser pool".
- Solves the escape problem for *manual* browsing already.

**Stage 2 — agent control via IDE-hosted MCP (SHIPPED 2026-06-16):**

- The IDE serves browser tools over local streamable-HTTP (`FastMCP` + uvicorn in a daemon thread, ephemeral port): `browser_navigate`/`eval_js`/`snapshot`/`click`/`fill`/`list_windows`. `eval_js` is the general primitive; `snapshot` tags interactive elements with a `data-sym-ref` that `click`/`fill` address (a v1 interactive-elements snapshot; a full a11y tree is a later refinement).
- Spawned agents auto-discover it via per-harness `--mcp-config` injection in `agent_harness.py` (claude; opencode's per-launch path is still open). Automation drives the `WebEngineView` via `runJavaScript`, delivered QML-side (the view isn't Python-drivable) and marshaled ONTO the GUI thread by `BrowserMcpBridge` (the inverse of gotcha #1).
- Modules: `browser_automation.py` (2a, the delivery substrate), `browser_mcp.py` (2b, the server + bridge), `agent_harness.py` (2c, injection). Non-fatal if `python-mcp` is absent or `SYMMETRIA_IDE_BROWSER_MCP=0`.

**Checkpoint (Stage 2) — MET:** a real MCP client drives `eval_js`/`navigate`/`list_windows` against a live embedded window end-to-end, with zero external Chromium. Deferred: agent-pane↔browser shared state (screenshots into agent history) and a full a11y-tree snapshot.

## Far future

- **Own window manager** — fork Hyprland (~2 years out, dependent on LLM advancement). May reopen the federation architecture question.
- **gpui migration** — far-future full-rewrite possibility; the 2026-06 Tauri rewrite was considered and reversed, so gpui remains the standing far-future candidate. See `docs/future.md` "gpui migration."
- **Own editor core** — long-arc end-state: progressively strip NeoVim until it is "just a buffer," then replace it with an own editor core. The engine (web vs native) is unsettled post-reversal. See `docs/future.md` "Own editor core."
- **Custom agent harness** — Phase 2's frontend is agent-agnostic at the IPC layer. Any prompt/response protocol (OpenCode, PyAgent, custom) can plug in.
