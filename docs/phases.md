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
