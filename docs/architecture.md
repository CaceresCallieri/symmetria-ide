# Architecture

## Monolith vs federation

Two architectural shapes were considered:

- **Monolith** — one IDE application hosts file manager drawer, editor wrapper, agent UI, embedded browser. Coherent UX, shared state.
- **Federation** — specialized apps (File Manager, editor wrapper, agent UI, browser) communicate over IPC.

**Current decision: Monolith.**

Federation is harder to keep coherent today: inter-app communication under Hyprland is primitive, and running five processes to achieve what one process can do adds complexity without commensurate value.

**Deferred reconsideration:** once a custom window-manager fork is viable (estimated ~2 years out), native inter-application protocols may make federation attractive again. See `future.md`. The monolith is current, not permanent.

## The embedding model

```
 Symmetria IDE  (Qt / QML window)
 │
 ├─ Native status bar           ← orchestrator.nvim capsules  (Phase 0 — DONE)
 │
 ├─ Editor pane
 │   └─ NeoVim TUI in a QMLTermWidget  ← pynvim RPC on --listen (chrome + control)  (Phase 0 — DONE)
 │
 ├─ File manager drawer         ← deferred pending QuickShell→Qt decision
 │
 ├─ Agent pane                                                 (Phase 2 — Node SDK sidecar landed)
 │   ├─ Node sidecar (sidecar/dist/index.js)   ← drives @anthropic-ai/claude-agent-sdk
 │   ├─ JSONL on stdin/stdout                  ← user_message / permission_response in; SDK events out
 │   ├─ Flat event ListView + permission card  ← canUseTool synthesizes permission_request
 │   └─ Inline image / HTML diagram renderers  ← deferred until placeholder UX is exercised
 │
 └─ Browser pane                ← QtWebEngine, cmux-pattern agent control     (Phase 4)
```

## Realized Phase 0 implementation

```
NeoVim TUI (drawn inside the editor QMLTermWidget — Konsole's VT engine renders nvim's own grid)
    │
    │  separately, on nvim's --listen socket:
    ↓
pynvim.Nvim (RPC-only client, worker thread — NO ui_attach, NO grid render)
    ↓ run_loop dispatches Lua-emitted rpcnotify payloads
    │
    └─ capsule notifications → capsule_updated signal
                                   ↓
                               AppController._route_capsule
                                   ↓
                          ┌────────┴────────┐
                  StatusBarState.apply   CapsuleModel.update
                  (mode/file/branch/     (unknown ids,
                   project/pos)           extensibility)
                          ↓
                     StatusBar.qml (bindings re-evaluate via per-property notify signals)
```

**Critical invariants (encoded in code):**
- Any RPC call from the Qt GUI thread MUST be marshaled via `nvim.async_call` — cross-thread calls raise `NvimError: request from non-main thread`.
- Our `runtime/init.lua` publishes `_G.symmetria_push_state()` so Python can force a re-push after subscribing to `"capsule"` (we race the plugin's initial push otherwise).
- `laststatus` / `showmode` must be re-asserted from a `VimEnter` autocmd — lualine setup clobbers them if we only set at `--cmd` time.
- QML bindings must depend on notifiable properties. A `Text { text: model.rowCount() }` computes once and stays stale; use per-field `@Property` with `notify=` signals instead.

## Progressive NeoVim extraction

The editor core (NeoVim buffer and window) stays untouched for years. Only the *chrome* migrates:

| NeoVim chrome            | Native replacement                              | Phase      |
|--------------------------|-------------------------------------------------|------------|
| Lualine / status line    | QML status bar fed by the capsule protocol      | 0 — DONE   |
| NeoVim `:` command line  | Native QML cmdline overlay + completions popup  | 0 — DONE   |
| which-key.nvim popup     | Native QML which-key overlay (whichkey protocol)| 0 — DONE   |
| LazyGit                  | Native QML agentic-git frontend                 | Later      |
| fff.nvim (fuzzy)         | Native finder (likely folds into File Manager)  | Later      |
| Editor buffer itself     | Eventually — possibly gpui-based                | Far future |

**The extraction mechanism is Lua `rpcnotify`, NOT NeoVim's `ui-ext`.** nvim runs as a TUI and draws its own grid inside the editor QMLTermWidget — Python never calls `ui_attach`, so `ext_cmdline` / `ext_messages` / `ext_popupmenu` / `ext_tabline` are not in play. Instead, Lua code in `runtime/init.lua` (and the `runtime/lua/orchestrator/` modules) observes nvim's state via autocmds and pushes structured payloads to Python over the `--listen` RPC channel with `vim.rpcnotify(0, "<channel>", {...})`. Three such channels feed the native chrome:

- **capsule protocol** (`"capsule"`) — mode / file / branch / project / cursor-position state → `StatusBarState` + `CapsuleModel`.
- **completions pipeline** (`"completions"`) — `getcompletion()`-derived cmdline completion lists → `CompletionModel`, bound by `CommandLine.qml`. (We run our own pipeline rather than `ext_popupmenu` so plugin popups don't draw at the default bottom-row cmdline position.)
- **whichkey protocol** (`"whichkey"`) — a trie built from `nvim_get_keymap` + presets → `WhichKeyState` + `WhichKeyModel`, rendered by `WhichKeyOverlay.qml`.

Python routes each channel to the matching QML-bound model. This keeps the editor core untouched: only the chrome's *presentation* moves to native QML, while nvim itself remains the authority for buffers, motions, and text rendering.

## Communication topology

```
 [Python backend]
    ├─ QML spawns → [nvim --listen]        (TUI inside the editor QMLTermWidget; draws its own grid)
    │                                       ↑ pynvim RPC-only client attaches here (control + chrome relays)
    ├─ QML spawns → [$SHELL]               (shell QMLTermWidget pane)
    ├─ spawns   → [node sidecar/dist/index.js]  (JSONL on stdin/stdout via session_host.py;
    │                                            sidecar drives @anthropic-ai/claude-agent-sdk)
    ├─ hosts    → orchestrator bridge      (reads capsule state via nvim RPC)
    └─ exposes  → QML signals / props      (backend ↔ UI data binding)

 [QML frontend]
    ├─ StatusBar.qml
    ├─ QMLTermWidget (editor)          (nvim TUI; Konsole VT engine renders the grid)
    ├─ QMLTermWidget (shell)           (interactive $SHELL pane)
    ├─ FileManager/                    (imported from Symmetria File Manager — Phase 1, deferred)
    ├─ AgentPane.qml                   (flat event list + inline permission card)
    └─ BrowserPane.qml                 (wraps QtWebEngineView — Phase 4)
```

## Realized Phase 2 (agent pane, Node SDK sidecar)

```
sidecar/dist/index.js (Node sidecar; @anthropic-ai/claude-agent-sdk)
    ↑ JSONL stdin: user_message / permission_response          (from Python via _stdin_lock)
    ↓ JSONL stdout: translated SDK messages + permission_request envelopes
SessionHost (daemon worker thread, mirrors NvimBackend shape)
    ↓ parse_jsonl_line → event dict
    ↓ gc.disable / enable around emit            (gotcha #10)
    ↓ event_received(dict)                       (Qt queued connection)
SessionModel (GUI thread, QAbstractListModel)
    ↓ apply() routes on top-level `type` discriminator
    ↓ append / extend-last-streaming-row + dataChanged(roles=[TextRole])
    ↓ permission_request → row with permission_state="pending"
AgentPane.qml (flat ListView with typed-property delegate; permission card variant)
    ↓ approve/deny click → controller.respond_to_permission(requestId, decision)
        → SessionHost.send_permission_response (sidecar resolves canUseTool promise)
        → SessionModel.resolve_permission       (row flips to approved/denied; scoped dataChanged)

Main.qml: editor/shell QMLTermWidget + AgentPane share mainContent (one central surface visible at a time, driven by controller.centralSurface), with StatusBar below
```

**Critical invariants that carried over from Phase 0:**
- Gotcha #10 applies to any worker thread that allocates during signal emission — `SessionHost._run_stdout_loop` suspends GC around every emit. Same pattern, same reason.
- Project-standards §4 P0: cross-thread connections use explicit `Qt.QueuedConnection` with a grep-able comment. `AppController.__init__` marks every agent-pane connect site.
- Project-standards §1 P0: every long-running thread is `daemon=True` AND carries an explicit `threading.Event` for cooperative shutdown. `SessionHost._stop_event` mirrors `NvimBackend._stop_event`.
- Gotcha #3: `dataChanged` for partial-text extension is emitted with an explicit role list scoped to `TextRole`, never an empty list.

## Keyboard handling

- Key events flow first to the focused pane's handler.
- Panes that host NeoVim motions (editor, agent history) route through a shared motion translator.
- Global shortcuts (pane switching, command palette) are claimed at the QML window root and never passed through to child panes when matched.
- NeoVim is the authority for text-surface motions. The wrapper does not reinterpret `hjkl` or similar.
