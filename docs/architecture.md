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
 ├─ Native status bar           ← orchestrator.nvim capsules                  (Phase 0)
 │
 ├─ Editor pane
 │   └─ NeoVim                  (headless, --embed, msgpack-RPC)
 │
 ├─ File manager drawer         ← Symmetria File Manager QML root             (Phase 1)
 │
 ├─ Agent pane
 │   ├─ pty + pyte              ← terminal emulation for Claude Code          (Phase 2)
 │   ├─ Warp-style block model  ← conversation history as navigable blocks
 │   └─ Inline image / HTML diagram renderers
 │
 └─ Browser pane                ← QtWebEngine, cmux-pattern agent control     (Phase 4)
```

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
