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
 ├─ Agent pane                                                 (Phase 2 — NEXT)
 │   ├─ pty + pyte              ← terminal emulation for Claude Code
 │   ├─ Warp-style block model  ← conversation history as navigable blocks
 │   └─ Inline image / HTML diagram renderers
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
    ├─ spawns  → [claude-code pty]     (ptyprocess + pyte)
    ├─ hosts   → orchestrator bridge   (reads capsule state via nvim RPC)
    └─ exposes → QML signals / props   (backend ↔ UI data binding)

 [QML frontend]
    ├─ NativeStatusBar.qml
    ├─ NeovimView.qml                  (renders nvim grid events)
    ├─ FileManager/                    (imported from Symmetria File Manager)
    ├─ TerminalView.qml                (renders pyte cell model)
    └─ BrowserPane.qml                 (wraps QtWebEngineView)
```

## Keyboard handling

- Key events flow first to the focused pane's handler.
- Panes that host NeoVim motions (editor, agent history) route through a shared motion translator.
- Global shortcuts (pane switching, command palette) are claimed at the QML window root and never passed through to child panes when matched.
- NeoVim is the authority for text-surface motions. The wrapper does not reinterpret `hjkl` or similar.
