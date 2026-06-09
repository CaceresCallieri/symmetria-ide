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

**On the more ambitious "command gate" elaboration (reversal note):** A stronger form of this direction — the IDE as a full command gate that intercepts every keypress first and falls unclaimed keys through to nvim — was sketched under the **now-reversed (2026-06-07)** Tauri pivot as a *web/DOM* gate (capture-phase before xterm.js). **That web framing is dead.** The custom Lua which-key (CLAUDE.md gotchas #15–#21) is **live, not deleted** — it remains the native QML which-key overlay, with which-key.nvim neutralized on VimEnter. If the command-gate elaboration is ever pursued post-reversal it would be a **native-QML** keymap mirror, and these design insights still carry:

- **`<leader>` would need to be born "unified" day one** — merge nvim's `<leader>*` subtree from the start, else claiming the leader *prefix* starves nvim's entire leader namespace (all-or-nothing per prefix). Leader bindings are all in `nvim_get_keymap` (gotcha #18 N/A); only gotcha #21 carries (re-query the leader subtree on the `LspAttach` tick). Intercept the physical leader for display only; replay the chosen full sequence over RPC.
- **Motion prefixes (`g`/`z`/`[`/`]`/`<C-w>`) stay 100% nvim** — the IDE never claims them; the native QML which-key overlay shows them. No mirror burden.
- **Mode-awareness is load-bearing** — the `mode` capsule is an input-routing signal: never intercept printable keys in insert mode; only claim unambiguous triggers (Ctrl-chords, leader). Same lesson-class as gotcha #19, at the IDE↔nvim boundary.

