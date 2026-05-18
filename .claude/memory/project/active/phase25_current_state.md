---
name: Phase 2.5 terminal pane + project anchor — current state
description: 2026-05-18 snapshot. Anchor (deliverable 1) shipped. Native PTY terminal pane (deliverable 2) — PRs 1–5 sealed; PR 6 (paste) is the final v1 step. OSC 7 shell integration (deliverable 3) is next.
type: project
originSessionId: phase25-anchor-spike
---

# Phase 2.5 — current state (2026-05-17)

Phase 2.5 = terminal pane + project anchor. The first concrete step toward the agent-primary topology inversion documented in `docs/future.md`. See `docs/phases.md` for the full phase entry with the three sub-deliverables and architectural invariants.

## Why Phase 2.5 exists (the framing that justified the spike)

The user's vision is a terminal-first IDE: launch the IDE → empty terminal as primary surface → navigate via shell → anchor → editor accessible. The terminal pane is the foundation; the anchor concept is the *seam* between "roaming" and "working on a project". Three architectural gaps separated today's IDE from that vision:

- **Gap 1 — Source is nvim's cwd, not the shell's.** Needs terminal pane + OSC 7 shell integration. Deferred to Phase 2.5 deliverable 2 + 3.
- **Gap 2 — No anchor state machine.** Every cwd update unconditionally retargets the file tree. **Closed by this spike.**
- **Gap 3 — No anchor/unanchor trigger.** **Closed by this spike** (Ctrl+Shift+A + user commands).

The spike deliberately built Gap 2 + Gap 3 in isolation so the abstraction is validated end-to-end via `:cd` inside nvim *before* investing in the terminal pane. When the terminal lands and starts pouring shell-driven cwd updates into `_cwd`, the anchor machinery on top works unchanged — that's the load-bearing claim this spike de-risked.

## What shipped (anchor state machine — deliverable 1)

### Python state machine (`src/symmetria_ide/app.py`)

- 3 new fields on `AppController`: `_anchored: bool`, `_anchored_root: str`, plus the existing `_cwd: str`.
- 2 new signals: `anchoredChanged`, `displayedRootChanged`.
- 2 new `@Property`: `displayedRoot` (returns `_anchored_root if _anchored and _anchored_root else _cwd` — pure derivation, never a stored field), `anchored` (bool).
- 3 new `@Slot`: `anchor_to_current_cwd`, `anchor_to_path(str)`, `release_anchor`.
- 1 new `@Slot(dict)`: `_on_anchor_event` — dispatches `op="set"` → `anchor_to_path` (or `anchor_to_current_cwd` if path absent) and `op="clear"` → `release_anchor`.
- **Load-bearing conditional in `_route_capsule`**: `cwd` capsules update `_cwd` unconditionally (so a later release re-syncs cleanly) but only emit `displayedRootChanged` when NOT anchored. The else-branch logs `"cwd update suppressed (anchored to %s)"` at DEBUG.
- **Git controller rebind (D2 decision)**: `self.cwdChanged.connect(self._sync_git_repo_root)` → `self.displayedRootChanged.connect(self._sync_git_repo_root)`. `_sync_git_repo_root` now reads `self.displayedRoot` instead of `self._cwd`. This is the one place where the anchor concept leaks below the pure view-transformation line into actual behavior — git operations target the anchored root even as raw cwd wanders.

### Lua user commands (`runtime/init.lua`)

- `:SymmetriaAnchor [path]` — emits `rpcnotify(0, "anchor", {op = "set", path = ...})`. With no arg, `path` is omitted and Python falls back to `anchor_to_current_cwd`. Tab-complete against directory names (`complete = "dir"`).
- `:SymmetriaUnanchor` — emits `rpcnotify(0, "anchor", {op = "clear"})`.
- **No `<leader>` keybind.** Anchor is an IDE-level concern, NOT a nvim concept — giving it a `<leader>` slot would mis-locate it.

### Python RPC routing (`src/symmetria_ide/nvim_backend.py` + `nvim_events.py`)

- New `anchor_event = Signal(dict)` on `NvimBackend`, mirroring `nav_event` / `tree_event` / `fm_event` / `agent_event`.
- New `"anchor"` dispatch branch in `_dispatch_notification` — same defensive shape as the other channels (non-dict payloads logged + dropped).
- `AppController.__init__` connects `self._backend.anchor_event.connect(self._on_anchor_event)`. Same-thread connect (no QueuedConnection needed) because anchor events arrive on the GUI thread already via `capsule_updated`-class routing.

### QML application-scope shortcut + rootPath rebind (`qml/Main.qml`)

- `FileTreeView.rootPath` rebound from `controller.cwd` → `controller.displayedRoot`. Single-line change; QML's binding system re-evaluates automatically on `displayedRootChanged`.
- New `Shortcut { sequences: ["Ctrl+Shift+A"]; context: Qt.ApplicationShortcut; onActivated: controller.anchored ? controller.release_anchor() : controller.anchor_to_current_cwd() }` at the Window root. **First IDE-level keybind in this codebase** — every prior chord (`<C-S-q>`, `<C-1>..<C-5>`, `<leader>aN`, `<leader>tf`, `<leader>e`) is a Lua keymap that only fires when nvim has focus. The `Qt.ApplicationShortcut` context resolves at `QApplication::notify` BEFORE focused-widget key handlers, so it wins over NvimView's keyboard capture even in insert mode.

### Tests

- **New `tests/test_anchor_state.py`** (13 tests): initial state, anchor/release transitions, idempotency (re-anchor same path, release while released), empty-path rejection, "anchor holds against cwd change" load-bearing canary, "release restores cwd tracking" pair, defense-in-depth empty-anchored-root fallback.
- **Extended `tests/test_nvim_backend_dispatch.py`** (3 tests): `anchor` channel set/clear/non-dict-drop coverage mirroring the `nav` channel tests.
- **Full suite**: 493 → 496 passing (anchor module + dispatch additions).

## Key decisions locked during the spike (referenced as D1–D5 in conversation)

- **D1** — Anchor state lives inline on `AppController` (not a separate `AnchorState` class) for the spike. The API surface is shaped so promotion to a dedicated class later is a one-pass refactor.
- **D2** — Git controller follows the anchored root (not raw cwd). Rationale: pinning git to the anchor IS the user-facing payoff of anchoring.
- **D3** — Status-bar `project` pill stays on raw cwd basename for now. Updating it would require intercepting + mutating the Lua-emitted `project` capsule in Python, which expands blast radius beyond the spike. Deferred.
- **D4** — New property name is `displayedRoot` (not rebinding `cwd`'s semantics). Keeps `cwd` available as the raw signal for any consumer that explicitly wants it.
- **D5** — No persistence across IDE restart in the spike. Adding $XDG_STATE_HOME plumbing is one follow-up commit later.

## Topology decision (Q2-d, confirmed during planning)

**Post-anchor terminal placement = full-window swap with terminal as persistent home, nvim summoned over it.** Inverse of how the agent pane works today (`<leader>aN` summons agent over editor). This decision shapes deliverable 2 (terminal pane) — it goes in the central layout slot, swappable with NvimView via a `terminalVisible` / `editorVisible` pair.

## Launch-state decision (Q1 answer 1b, confirmed)

**Pre-anchor: nvim is pre-spawned in the background**, hidden, so post-anchor handoff is instant. The pragmatic compromise version of the long-term vision — eventually nvim becomes summoned-on-demand only, but for now it carries enough daily-driver weight to justify the eager-load.

## What shipped — Deliverable 2 (native PTY terminal pane)

Sealed through 5 of 6 PRs in this session (2026-05-17 → 2026-05-18). See `git log --oneline | grep terminal:` for the commit ladder.

- **PR 1** (`ac71dc4` + `d64c3e6`) — pyte>=0.8.2 dep, Theme.color.terminal palette, TerminalBackend skeleton. Fix-commit hardened `screen_dirty` to `Signal(frozenset)` (Qt QueuedConnection passes set by ref — frozenset closes the gotcha #10 race).
- **PR 2** (`a9da560` + `df3657e`) — full TerminalBackend impl: os.openpty + subprocess.Popen with `start_new_session=True` + login shell + xterm-256color env, pyte HistoryScreen + ByteStream, daemon reader thread with select+self-pipe, GC-suspended emit window, killpg shutdown with SIGTERM→SIGKILL grace, TIOCSWINSZ ioctl. Fix-commit swapped `preexec_fn=os.setsid` for `start_new_session=True` (Python fork-safe equivalent — no PLW1509 noqa needed) and dropped a `time.sleep(0.2)` from the round-trip test (§8 violation; suite 8.7s→2.6s).
- **PR 3** (`c261563` + `0051e83`) — TerminalView QQuickPaintedItem renderer + terminal_keys.py xterm escape translator. Paint loop with run-coalescing + memoized QColor + pooled QRectF + grid-exact clip (gotchas #10/#11). Reuses `NvimView._default_font()` for cell-metric alignment (gotcha #23). 16-slot ANSI palette mirroring Theme.qml with drift-detection test. Fix-commit extended palette cross-check to all 16 slots + documented v1 run-coalesce key omissions (underscore/strikethrough/blink not rendered yet).
- **PR 4** (`c99ef80` + `35888f5`) — AppController integration: centralSurface state machine, swap_to_terminal / swap_to_editor / focus_terminal slots, start() pre-warms terminal AFTER nvim (Q1-1b), shutdown() stops terminal BEFORE nvim, terminalBackend context property. Fix-commit corrected the shutdown ordering comment (the real rationale is signal-race prevention, NOT "event loop is healthy" — nvim's stop blocks in threading.join, not Qt exec) + hardened test fixture with env-var isolation.
- **PR 5** (current) — Main.qml wiring: TerminalView sibling under `mainContent` Item gated on `controller.terminalVisible`, NvimView visibility tightened to `!agentVisible && editorVisible`, two new `Qt.ApplicationShortcut` blocks (Ctrl+Shift+T / Ctrl+Shift+E), `onFocusTerminalRequested` Connections handler, `Window.onActiveChanged` + `Component.onCompleted` focus dispatch extended for three-way central state, FM overlay restore-target extended. Plus docs: CLAUDE.md "The terminal pane" section, this memory file, CHANGELOG entry.

**v1 deferrals** (documented at the call site so future agents have the breadcrumb):
- Application-mode arrow keys (DECCKM). Vim/less flip it on entry — their own arrow handling masks the difference at the shell level.
- Selection / copy. Would need a vim-style visual mode to honor the keyboard-first non-negotiable; mouse selection violates it.
- Underline / strikethrough / blink rendering. A future PR adding any of these MUST extend the `(fg, bg, bold, italic)` run-coalescing key in `_paint_row`, otherwise adjacent cells with different attribute states silently corrupt via shared run.
- Partial repaints via `update(QRect)`. v1 full-repaints on every `screen_dirty`; the carried payload is structurally advisory until a v2 consumer uses it.

## What's next

- **PR 6** — `Ctrl+Shift+V` paste. Reads `QApplication.clipboard().text()`, encodes UTF-8, calls `terminalBackend.write(bytes)`. ~10 lines + 1 test. Final v1 deliverable.
- **Deliverable 3 — Shell-driven cwd integration.** Inject `chpwd` hook into user's shell emitting OSC 7. Terminal pane parses, pushes into existing `cwd` capsule pipeline. Anchor machinery on top works unchanged. Sidecar work, NOT yet started.

## Load-bearing invariants (don't regress)

- **`_cwd` is the raw signal; `displayedRoot` is a view transformation.** They MUST stay separate fields/properties. The terminal pane (deliverable 2) will pour shell cwds into `_cwd`; the anchor still pins what the UI displays.
- **The conditional `if not self._anchored: self.displayedRootChanged.emit()` in `_route_capsule`'s cwd branch is the keystone of the state machine.** Removing it = anchoring degrades to a no-op (every BufEnter re-fires downstream binds). `tests/test_anchor_state.py::test_anchor_holds_against_cwd_change` is the canary.
- **Git controller MUST stay connected to `displayedRootChanged`, not `cwdChanged`.** Reverting that connect = anchoring loses its user-facing payoff (git operations stop targeting the anchored root).
- **Anchor triggers live at the Qt application-scope shortcut layer, NOT in Lua keymaps.** Anchor is IDE-level. Adding a `<leader>ta` keybind would mis-locate it and the architectural intent erodes.
- **The QML `Shortcut` with `Qt.ApplicationShortcut` context is the established pattern for IDE-wide keybinds.** Future IDE-wide chords (terminal-focus in deliverable 2, project-switcher, etc.) should follow the same template — `Shortcut { context: Qt.ApplicationShortcut; onActivated: controller.<slot>() }` at the `Main.qml` Window root.
- **`_central_surface` is a single string, the two derived booleans are XOR by construction.** `centralSurface` (str), `editorVisible` (bool), `terminalVisible` (bool) all read the same field via `@Property(..., notify=centralSurfaceChanged)`. A future refactor that splits them into stored fields would lose this guarantee — keep them derived.
- **start() ordering: nvim FIRST, then terminal.** nvim's start gates the QSGRenderThread's first frame; spawning terminal first can briefly flash an empty editor on slow hardware. The OSError swallow on terminal spawn is intentional (editor stays usable when shell binary is missing).
- **shutdown() ordering: terminal FIRST, then nvim.** Terminal owns a shell process group via `start_new_session=True`. killpg reaping should complete before nvim's stop blocks the controller in `threading.join()`; the ordering also prevents the terminal reader thread's queued signals from landing against a mid-nvim-teardown scene graph.
- **ANSI palette is a dual source of truth (Python `_ANSI_PALETTE` ↔ QML `Theme.color.terminal.*`).** `test_terminal_view.py::test_ansi_palette_matches_theme_qml` reads Theme.qml and cross-checks every slot. The v2 refactor that wires Theme through Python via context property removes the duplication; until then, palette nudges need both sides updated.
