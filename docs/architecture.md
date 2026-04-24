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
 │   └─ NeoVim                  (--embed, msgpack-RPC)        (Phase 0 — DONE)
 │
 ├─ File manager drawer         ← deferred pending QuickShell→Qt decision
 │
 ├─ Agent pane                                                 (Phase 2 — placeholder spike landed)
 │   ├─ claude -p --output-format stream-json  ← subprocess + JSONL event stream
 │   ├─ Flat event ListView (placeholder)      ← turn grouping + drill-in follow
 │   └─ Inline image / HTML diagram renderers  ← deferred until composer lands
 │
 └─ Browser pane                ← QtWebEngine, cmux-pattern agent control     (Phase 4)
```

## Realized Phase 0 implementation

```
NeoVim (--embed child process)
    ↓ msgpack-RPC over stdio
pynvim.Nvim (worker thread)
    ↓ run_loop dispatches redraw + capsule notifications
    │
    ├─ redraw batches → Grid (pure Python, 2-D Cell array)
    │                       ↓ flush
    │                   redraw_flushed signal → NvimView.update()
    │                                              ↓ paint()
    │                                          QQuickPaintedItem
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

| NeoVim chrome            | Native replacement                             | Phase      |
|--------------------------|------------------------------------------------|------------|
| Lualine / status line    | QML status bar with orchestrator capsules      | 0          |
| NeoVim `:` command line  | Native QML command palette via `ext_cmdline`   | 3          |
| LazyGit                  | Native QML agentic-git frontend                | Later      |
| fff.nvim (fuzzy)         | Native finder (likely folds into File Manager) | Later      |
| Editor buffer itself     | Eventually — possibly gpui-based               | Far future |

NeoVim's `ui-ext` capabilities (`ui_attach` with `ext_cmdline`, `ext_messages`, `ext_popupmenu`, `ext_tabline`) are the formal hooks for this extraction. Use them deliberately for the pieces we are extracting — they cost FPS if overused (lesson from goneovim).

## Communication topology

```
 [Python backend]
    ├─ spawns  → [nvim --embed]        (msgpack-RPC over stdio, via pynvim)
    ├─ spawns  → [claude -p ... stream-json]  (subprocess + JSONL events via session_host.py)
    ├─ hosts   → orchestrator bridge   (reads capsule state via nvim RPC)
    └─ exposes → QML signals / props   (backend ↔ UI data binding)

 [QML frontend]
    ├─ StatusBar.qml
    ├─ NvimView                        (renders nvim grid events; QQuickPaintedItem)
    ├─ FileManager/                    (imported from Symmetria File Manager — Phase 1, deferred)
    ├─ AgentPane.qml                   (flat stream-json event list; placeholder)
    └─ BrowserPane.qml                 (wraps QtWebEngineView — Phase 4)
```

## Realized Phase 2 placeholder (agent pane, stream-json pivot)

```
claude -p --output-format stream-json --include-partial-messages
    ↓ subprocess stdout (JSONL, one event per line)
SessionHost (daemon worker thread, mirrors NvimBackend shape)
    ↓ parse_stream_json_line → event dict
    ↓ gc.disable / enable around emit            (gotcha #10)
    ↓ event_received(dict)                       (Qt queued connection)
SessionModel (GUI thread, QAbstractListModel)
    ↓ apply() routes on top-level `type` discriminator
    ↓ append / extend-last-streaming-row + dataChanged(roles=[TextRole])
AgentPane.qml (flat ListView with typed-property delegate, Theme-tokens only)

Main.qml: ColumnLayout { RowLayout { NvimView(60%), AgentPane(40%) }, StatusBar }
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
