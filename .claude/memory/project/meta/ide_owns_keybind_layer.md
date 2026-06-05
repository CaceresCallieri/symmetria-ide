---
name: ide-owns-the-keybind-layer-nvim-terminal-are-bare-engines
description: "Strategic direction — Symmetria IDE is the canonical UX surface; embedded nvim and terminal are progressively stripped of their own keybinds so the IDE provides one uniform, consistent set"
metadata: 
  node_type: memory
  type: project
  originSessionId: 24d3d78f-ecfb-4678-b251-3411e2a2621a
---

**Direction:** As Symmetria IDE matures, more and more keybinds and UI surfaces get reclaimed from the embedded nvim and terminal pane and re-implemented at the IDE level. The end state is nvim and the terminal being "almost bare" engines — used for their core capabilities (motions, PTY rendering) — while Symmetria IDE owns the user-facing chord layer, navigation model, and visual chrome.

**Why:** The user explicitly articulated this on 2026-05-20 while designing the `Ctrl+H` / `Ctrl+L` pane-navigation chord. The goal is a *consistent and uniform system of keybinds and exploration and design* across all panes — one mental model regardless of which engine is mounted underneath. This is the same architectural inversion `docs/future.md` calls **Topology inversion**: IDE stops being a nvim host and becomes the canonical surface, with nvim/terminal demoted to pluggable engines.

**How to apply:**

- When a new IDE-level chord conflicts with a default nvim or terminal binding, **the IDE wins** — capture as `Qt.ApplicationShortcut` at `Main.qml`'s Window root, do NOT delegate down to nvim's `wincmd` / terminal's keyhandling. The chord has *one* meaning regardless of focus.
- nvim's own prefix-style chords (`<C-w>h`, `<leader>…`, `:` commands) remain untouched because we don't capture the prefix — users who want nvim-internal split navigation still have `<C-w>h/l` available, just not bare `<C-h>/<C-l>`.
- When in doubt about which side owns a piece of UX, **default to the IDE**. Keybind cost in the underlying engine (e.g., terminal loses `Ctrl+H` = ASCII Backspace) is the price of admission; flag it explicitly when introducing the chord but don't let it block the move.
- Don't ask "should we delegate to nvim's wincmd / equivalent?" each time — the answer is no. The reflexive delegate-to-engine instinct is what this principle exists to override.
- This applies to chords *and* to surface ownership in general: status line, command line, which-key, file tree, completion popup are all already reclaimed (see CLAUDE.md "The capsule protocol", "The completion pipeline", "The which-key protocol"). Future reclaim candidates: agent pane chrome, terminal scrollback navigation, search/replace UX.

**Precedent already in tree:**

- `Ctrl+Shift+T` / `Ctrl+Shift+E` / `Ctrl+Shift+A` — IDE-wide surface-swap chords at `Main.qml` Window root, win over nvim insert-mode capture (commit `c6e003b`).
- Native status bar, command line, which-key overlay, completion popup — all built at IDE level, with their nvim/plugin counterparts neutralized on `VimEnter`.

**Full expression under the framework pivot ([framework_pivot](../active/framework_pivot.md), `docs/framework-pivot.md` §4):** which-key is elevated from "a native overlay mirroring nvim's keymap" to **the IDE-level command gate** — every keypress hits the IDE first (DOM capture-phase, before xterm.js), IDE-bound actions run in the IDE, unclaimed keys fall through to nvim (via the `--listen` RPC channel or PTY). Precedence contract:

- **`<leader>` is born in "B" (unified) day one** — the IDE overlay must merge nvim's `<leader>*` subtree from the start, else claiming the leader *prefix* starves nvim's entire leader namespace (it's all-or-nothing per prefix). Cheapest slice of B: leader bindings are all in `nvim_get_keymap` (no built-in motions → gotcha #18 N/A); only gotcha #21 carries (re-query leader subtree on `LspAttach` tick). Leave nvim's leader maps installed; intercept the physical leader for display only; replay the chosen full sequence over RPC.
- **Specific Ctrl-chords = pure "A" forever** (each owned by one side, rest falls through).
- **Motion prefixes (`g`/`z`/`[`/`]`/`<C-w>`) stay 100% nvim** — IDE never claims them; which-key.nvim shows them in-terminal. No mirror burden.
- **Mode-awareness is now load-bearing:** the `mode` capsule becomes an input-routing signal — never intercept printable keys in insert mode; only claim unambiguous triggers (Ctrl-chords, leader). Same lesson-class as gotcha #19, now at the IDE↔nvim boundary.

The custom Lua which-key (CLAUDE.md #15–#21) is *deleted* in the pivot, not ported — but its data-gathering insights (#18, #21) inform the eventual full-B keymap mirror.

