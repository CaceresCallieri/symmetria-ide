# Phases

Each phase ends with a go/no-go checkpoint. If a phase's deliverable does not feel right, reconsider before continuing.

## Phase 0 — Spine  *(current)*

**Goal:** one PySide6 window embeds NeoVim and renders one `orchestrator.nvim` capsule in a native QML status bar.

**Deliverables:**

- PySide6 app window with a QML root.
- `nvim --embed` spawned; msgpack-RPC wired via `pynvim`.
- NeoVim grid rendered into a QML view (minimum: one buffer, cursor, basic text attributes).
- Native QML status bar.
- One `orchestrator.nvim` capsule's state bridged from NeoVim to the status bar via autocmd → RPC notification → QML `ListModel`.

**Checkpoint:** Does it feel fast? Does the aesthetic match Symmetria? If yes, continue.

## Phase 1 — File Manager integration

**Goal:** the Symmetria File Manager runs as a panel inside the IDE window and opens at the focused NeoVim project root.

**Deliverables:**

- Import the File Manager QML root as a child component.
- Bridge: NeoVim CWD → File Manager `currentDirectory`.
- Toggle / show / hide keybindings.
- File selection in the File Manager feeds a path back to the agent pane.

**Checkpoint:** can the full File Manager workflow (including fuzzy search) happen inside the IDE window with no regression?

## Phase 2 — Claude Code frontend  *(biggest payoff)*

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

**Goal:** extract NeoVim's native command line into a native QML command palette.

**Deliverables:**

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
