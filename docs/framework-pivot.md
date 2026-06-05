# Framework pivot: PySide6/QML → Tauri 2 (Rust + React/TS)

**Status:** Decided (direction), 2026-06-05. Execution not yet started.
**Supersedes:** the "PySide6 primary / gpui long-term" decision in `docs/tech-stack.md` and the "renders in QML" framing throughout the docs.
**This file is the north star for the pivot.** Other docs point here; where they conflict, this wins.

---

## One-paragraph summary

Symmetria IDE pivots its **wrapper** (everything that is *not* the editor engine) from PySide6 (Qt6 + Python + QML) to **Tauri 2 (Rust core + React/TypeScript frontend)**, in the spirit of the Terax project. NeoVim stops being rendered by a custom `ext_linegrid` grid painter and instead runs as a **TUI inside an xterm.js terminal** (no custom scroll/cursor animations). A second `nvim --listen` RPC channel preserves the capsule/chrome/OSC-7 integration. which-key is **elevated to an IDE-level command gate** that owns the keymap and delegates unclaimed keys down to NeoVim. The driving forces are **development velocity** (TypeScript/React is a far better AI-codegen and library target than QML), an **agent-frontend ecosystem to lean on**, a **native embedded browser** for agent-driven web work, and **smaller bundles**. The long arc progressively strips capabilities out of NeoVim until it is "just a buffer," then replaces it with an own web editor (Monaco/CodeMirror) carrying vim-style navigation (flash-like motions).

---

## 1. The reframe that makes this coherent

**Symmetria IDE is a *wrapper*, not the editor.** The editor is a **swappable embedding**:

- Today: real NeoVim.
- Transition: NeoVim-in-a-terminal, progressively hollowed out.
- End state: an own web editor (Monaco / CodeMirror 6) with vim-style navigation plugins.

The *wrapper* is everything around that embedding: file tree, git status, file-manager integration, the Claude Code / multi-agent panel, future lazygit-style git tooling, the embedded browser, the which-key command gate, the side panels. **The wrapper is what moves to Tauri/web.** The editor's *experience* is preserved across the transition; its *renderer* is not (see §3).

This reframe matters because it dissolves the apparent contradiction "keep NeoVim but go web": you keep the NeoVim *engine and the user's real config*, you drop the *custom Qt renderer*.

## 2. The decision and why

**Adopt Tauri 2 (Rust core + React/TypeScript + Vite + Tailwind/shadcn-style UI) for the wrapper.** Reference baseline: **Terax** (`crynta/terax-ai`, Apache-2.0) — a 7-8 MB Tauri-2 "AI terminal" that validates the integrated terminal + editor + file-tree + AI form factor and whose PTY/shell-integration layer is liftable with attribution (§11).

Why, in priority order:

1. **Development velocity + AI codegen.** TypeScript/React is the best-resourced codegen target there is; QML is a niche language that LLMs handle poorly. This is not opinion — Qt itself acknowledges it (their blog notes standard benchmarks "do not help measuring QML code quality," and they fine-tuned a bespoke `CodeLlama-13B-QML` model because stock models underperform). The five Qt-6.11 QML pitfalls already in CLAUDE.md memory are this tax made concrete. For an AI-assisted workflow this is the single biggest lever.
2. **Agent-frontend ecosystem to lean on.** Mature React Claude-Code UIs exist to study/harvest (e.g. `siteboon/claudecodeui` — multi-agent: Claude/Cursor/Codex/Gemini; note AGPL + CLI-based, so harvest patterns, don't fork wholesale onto our SDK sidecar). Multi-agent (OpenCode, Pi, …) is the web ecosystem's strong suit.
3. **Native embedded browser.** A Tauri app *is* a WebView, so hosting a browser pane — for Claude Code's "open a browser" actions, web preview, agent-driven navigation that stays *inside* the IDE and doesn't disturb the layout — is nearly free. In Qt the same feature means pulling in QtWebEngine (a whole bundled Chromium). This is a genuine platform-level win and a first-class feature of the new vision.
4. **Library ecosystem + references.** The web ecosystem (styling, components, editors, terminal) is vastly larger; plus Terax and other open projects to lean on for features.
5. **Bundle size.** Tauri's system-WebView model yields ~single-digit-MB bundles vs Qt's footprint. (Runtime RAM is *not* a guaranteed win — see §9.)

## 3. What changes for the editor

**Drop the custom grid renderer and animations. Run NeoVim as a TUI inside xterm.js.**

- The current `nvim_view.py` (a `QQuickPaintedItem` consuming `ext_linegrid` and painting cells, ~1,800 LOC) plus the `ScrollAnimation`/`CursorAnimation`/`CursorBlink` springs (CLAUDE.md gotchas #10–#14, #22) are **retired**. Porting Neovide-grade animation to a web canvas is the one part with *no living precedent* (every web nvim-GUI that attempted it — uivonim, NyaoVim — is dead; the survivors that nail it, Neovide/goneovim/Veil, all chose native GPU). We deliberately decline that work.
- Instead NeoVim runs as a normal terminal program inside an **xterm.js** pane (the same terminal tech used for shell terminals — one terminal stack for everything). NeoVim draws its own grid, cmdline, popupmenu, statusline, and any plugin float to the terminal, exactly as in a real terminal.
- **The NeoVim experience is fully preserved**: the user's real `~/.config/nvim`, plugins, LSP, treesitter, colorscheme, keymaps — all load and work, because the embedding is unchanged at the engine level. What is lost is the custom smooth-scroll/cursor *animation* and the native-overlay chrome (see below) — a deliberate trade for "usable ASAP" and because the editor is on a path to replacement anyway.

### What this *also* retires (and why that's good)

The terminal model deletes most of the project's hardest, most-fragile subsystems:

- **The entire custom which-key system** (`runtime/lua/orchestrator/whichkey/**`, ~1,200 LOC) + `WhichKeyOverlay.qml`. The IDE-level gate (§4) replaces it; nvim-level discovery is left to the real `which-key.nvim` in-terminal. CLAUDE.md gotchas #15–#21 (the `getcharstr` deadlock, keymap-clobber/restore, `nowait` latency) cease to exist — they were artifacts of running a modal loop *inside embedded Lua*.
- **The custom completion pipeline + `CommandLine.qml`.** Its whole reason to exist (extracted `ext_cmdline` made cmp/wilder draw floats at the wrong position — gotcha #8) evaporates: in a real terminal nvim draws its cmdline/popups in the right place natively.
- **The pyte answerback/DSR work** (the `post_tiocsctty_lag` memory) — xterm.js handles DA/DSR natively.

**Philosophy flip:** today the IDE *fights* the plugin ecosystem (neutralizing which-key.nvim, disabling cmp's cmdline, hiding lualine, guarding noice). In the terminal model it *embraces* it — telescope, which-key, noice, lualine all "just work" as in a normal terminal. This is strictly more robust, a permanent maintenance dividend, and *more* faithful to design principle #4 ("compose, don't reimplement") — the custom grid renderer was the one place we reimplemented what a terminal already does.

### Preserving the chrome: the `--listen` side channel

The side panels (location header, git status, mode/branch info — the capsule protocol, OSC-7 cwd sync) are what make the IDE more than a terminal, and they **survive**:

- Run NeoVim in the PTY for xterm.js display **and** start it with `--listen <socket>` so the Rust/Python core attaches a **second RPC connection** purely for data. NeoVim supports multiple simultaneous UIs/connections.
- The existing `init.lua` capsule emitter keeps firing `rpcnotify` over that channel and drives the **native web side panels** — independent of the grid being terminal-rendered.
- As git/file-tree/search migrate *out* of nvim into dedicated web tools, dependence on this channel shrinks anyway.

## 4. which-key as the IDE-level command gate

which-key stops being a NeoVim feature and becomes the **IDE's universal command gate** — the full expression of the existing `ide_owns_keybind_layer` principle.

- Every keypress hits the **IDE layer first** (DOM `keydown`, capture phase, *before* xterm.js sees it — the web analog of the `QApplication::notify` ordering used today for `Ctrl+Shift+A/E`).
- If the IDE has an action bound → IDE handles it (and shows it in the which-key overlay).
- If not → it **falls through to NeoVim**.
- Over time actions migrate *upward* (nvim → IDE); which-key is the single discoverability + dispatch surface for all of them, regardless of which engine executes.
- Two dispatch transports: IDE actions run in React/Rust directly; delegated actions go to nvim via the **RPC side-channel** (`nvim_input` / `nvim_command`, preferred for menu-selected actions) or via PTY passthrough (for keys the IDE never claims).

This is structurally **VS Code's / Zed's keybinding system** (command registry + precedence resolver + `when`-clause contexts + a which-key-style hint overlay) — a heavily-trodden, well-AI-supported pattern in TS.

### The precedence contract (write this into the implementation)

The fork is **A (IDE-actions-only) evolving to B (unified overlay mirroring nvim's keymap)** — but with one critical refinement, because plain A makes the NeoVim leader namespace *dead*:

- **`<leader>` (Space): born in B, day one.** Claiming a *prefix* is all-or-nothing — the moment the IDE swallows `<leader>` to open its overlay, nvim receives nothing under that prefix and all leader bindings die. Fix: the IDE overlay **merges nvim's `<leader>*` subtree** from the start. This is the *cheapest* slice of B: nothing under `<leader>` is a built-in motion, so it's all in `nvim_get_keymap`/`nvim_buf_get_keymap` — **gotcha #18 (preset catalog for built-in motions) does NOT apply**. Only **gotcha #21** carries (LSP binds leader keys late on `LspAttach` → re-query the leader subtree on a tick). Delegation: leave nvim's leader mappings installed; the IDE intercepts the *physical* leader only for display, and on selection replays the full sequence to nvim over RPC (`nvim_input("<Space>x")`), where nvim's own mapping fires. No remapped leaders, single muscle memory preserved.
- **Specific Ctrl-chords: pure A, forever.** Each chord is owned by exactly one side; the IDE claims what it wants, the rest falls through. No collision, no rush.
- **Motion prefixes (`g`/`z`/`[`/`]`/`<C-w>`) and all other nvim keys:** never claimed by the IDE; nvim + which-key.nvim own them in-terminal. No collision, no mirror burden. (These are exactly where the hard gotcha #18 preset problem lives — and we simply never touch them.)
- **"Evolve toward full B"** = absorb more namespaces into the IDE overlay *as actions migrate upward*. It gets cheaper over time because nvim shrinks toward "just a buffer."

### Mode-awareness is now load-bearing

NeoVim is modal. In **insert** mode the IDE must NOT intercept printable keys (`f` is just typing); in **normal/visual** mode it can claim its chords/leader. So the `mode` capsule graduates from status-bar decoration to an **input-routing signal**. Watch the async race: if the IDE's mode view lags a keystroke it could mis-route. Safe rule: only ever intercept *unambiguous* triggers (Ctrl-modified chords, the leader); always let printable keys flow through in insert mode.

This is the same class of problem as gotcha #19 (menu keymaps clobber the keymaps underneath), now living at the IDE↔nvim boundary instead of inside nvim — the lesson survives even though the code doesn't.

## 5. Transition architecture (Python sidecar now, Rust later)

**Decided:** keep the existing Python backend as a **sidecar** the Tauri shell talks to; collapse into a Rust core once the stack is proven.

```
Phase now (fastest to a running IDE):

  nvim (PTY)  ──► xterm.js (editor pane)        ┐
  nvim --listen ──msgpack-RPC──► Python backend │  (UNCHANGED: grid model where still needed,
                                  (capsules,     │   capsule emitter, OSC7, completion logic,
                                   OSC7, agent    │   session orchestration)
                                   orchestration) │
                                       │ JSONL/socket  ◄── pattern already exists
                                       ▼               (term_repl.py, session_host.py)
                               Tauri (Rust shell) ──IPC──► React/TS frontend
                                                            └─ file tree, git, agent pane,
                                                               browser pane, which-key gate,
                                                               side panels

Later (cleaner end state):
  Rust core drives nvim via nvim-rs, owns PTYs via portable-pty (lift Terax's layer),
  spawns the existing Node agent sidecar. Python eliminated.
  End-state languages: Rust + TS + Lua (+ Node agent sidecar).
```

The Node agent SDK sidecar is **already TypeScript** and carries over unchanged (the SDK is Node-only; Rust/Tauri spawns it exactly as Python does today).

## 6. Migration surface — what survives, what dies

From the codebase audit (≈35K LOC total; ≈20% portable logic, ≈80% Qt-bound rendering/models):

**Survives wholesale (copy/translate):**
- The **Lua runtime** (`runtime/init.lua`, `minimap.lua`, and the capsule/OSC7 emitters) — frontend-agnostic RPC; the which-key Lua is retired (§3), not ported.
- `grid.py` (pure data model) — only if a grid model is still needed; in the terminal model xterm.js owns the grid, so this may retire too.
- Spring/blink math — **deliberately dropped** with the animations.
- The **Node agent sidecar** — already TS.
- The **wire protocols** (capsule, OSC7, agent JSONL) — message routing, frontend-agnostic.

**Dies / fully rewritten:**
- `nvim_view.py` + `terminal_view.py` paint loops (replaced by xterm.js).
- All QML UI (`Main.qml`, `AgentPane.qml`, `StatusBar.qml`, `CommandLine.qml`, `WhichKeyOverlay.qml`, `GitStatusPanel.qml`, `Theme.qml`) → React/TS.
- All `QAbstractListModel`s (`SessionModel`, completion, etc.) → React state.
- Qt Signal/Slot wiring → JSONL/socket bridge (Path-1) then Rust IPC.

**Knowledge that survives even where code doesn't:** the which-key data-gathering insights (gotchas #18/#21) inform the eventual full-B mirror; the keymap-ownership lesson (#19) informs the IDE↔nvim precedence contract; the GC/paint-allocation discipline (#10) generalizes to "don't allocate per-cell per-frame" on any renderer.

## 7. Roadmap (wrapper-first, by risk)

1. **File manager** (Tauri/React, standalone-capable). The IDE's **file tree + git status derive from it** (shared components). Zero nvim-rendering conflict — pure UI, the ideal first move. Recreate the Symmetria look in CSS here; validates the stack on a cheap-to-fail surface.
2. **Agent panel + logic** (Claude Code frontend; harvest patterns from web Claude UIs; keep our SDK sidecar's `canUseTool` permission model — do not regress to CLI-based permission gating). Includes the embedded browser pane (§2.3).
3. **Git system** (lazygit-style native git tooling).
4. **which-key IDE gate** (§4) — woven through the above as the keybinding substrate.
5. **Editor pane in web** (the nvim-in-xterm.js integration into the unified window) — last, because it's the only piece with an architectural decision (it can be deferred while the wrapper tools ship as standalone apps first).

## 8. Long-term arc + identity shift

Progressive stripping: file tree out → shared file searcher out → lazygit out → orchestrator/agent logic out → **NeoVim becomes "just a buffer visualization"** → swap for an own web editor (Monaco/CodeMirror) with vim-style navigation (flash-like motions) and a few ergonomic plugins.

**This is a deliberate product-identity shift.** Two non-negotiables change meaning:
- **"NeoVim motions are sacred"** → **"vim-style navigation is preserved."** The end-state editor is an *emulation* layer; the "your real `~/.config/nvim` just works" promise sunsets when nvim becomes a buffer view and is then replaced.
- **"Renders in QML for aesthetic continuity"** → **"renders in web; aesthetic continuity is recreated in CSS."** The *look* is reproducible; only the *feel* may differ slightly.

Design principles #4 (compose, don't reimplement) and #6 (opinionated personal tooling) are *unchanged and reinforced*.

## 9. Honest caveats (do not bank the decision on the wrong premise)

- **Runtime RAM is NOT a win — MEASURED (2026-06-05).** Empirical PSS on this machine: **Terax core (app + WebView) = 213 MB** (`terax` Rust 49 + **WebKitWebProcess 161** + net 3); **Symmetria IDE fresh/idle = 217 MB** (`python`/Qt+QML 178 + `nvim` 36 + shells 3). They are **within 2%** — the "Symmetria IDE is much heavier than Terax" premise is **false**. The IDE figure is even understated (its agent sidecar didn't pre-warm — no `node` in the tree; a loaded IDE would be ~40–70 MB *higher*, i.e. above Terax). Reason: each stack has a heavy irreducible baseline — WebKitGTK's WebView (161 MB) for Terax, Qt+Python (178 MB) for Symmetria. The pivot replaces the 178 MB Python/Qt process with Rust (~49) **+ a 161 MB WebKitGTK WebView ≈ 210 MB** → a **wash, trending slightly worse**. Tauri's lightness is *bundle size* (system WebView), not runtime RAM; the "50% less than Electron" figures are vs bundled Chromium, mostly on Windows. **RAM is therefore NOT a valid reason for the pivot** — the justification rests on dev-velocity / AI-codegen / agent-ecosystem / embedded-browser / bundle-size. The only real RAM lever is dropping Python + consolidating to a Rust core (Path-2), and even then WebKitGTK's ~160 MB WebView stays.
- **WebKitGTK on Wayland/Hyprland — SPIKED on this machine 2026-06-05, result FAVORABLE.** A throwaway Tauri 2.11 app (transparent/decorationless window, canvas animation, in-page blur, iframe, fonts) was built and screenshot-verified via `grim` on this exact NVIDIA-Optimus Hyprland laptop (RTX 5070 Max-Q + AMD 860M, compositor on iGPU). **Default rendering (no env vars): transparency WORKS (desktop visible through the window — no black box), backdrop-filter blur WORKS, canvas 60 fps (vsync-capped; 104 fps uncapped → renderer has headroom), fonts crisp at all weights.** The feared NVIDIA-hybrid transparency/black-box failure **did not occur**. Notably, the documented workaround (`WEBKIT_DISABLE_DMABUF_RENDERER=1` + `WEBKIT_DISABLE_COMPOSITING_MODE=1`) **made it WORSE** — it broke backdrop-filter blur and turned translucent panels opaque-black. **Conclusion: ship default rendering; do NOT apply the DMABUF/compositing kill-switch** (it kills the Symmetria blur aesthetic). Residual unknowns: behavior under PRIME offload / external monitor on the NVIDIA GPU was not tested; and remote-URL iframe loading did not complete in the spike (a data: URL rendered fine, proving the rendering pipeline — but external load was inconclusive, confounded by the test launcher's possible network sandbox; verify deliberately + expect to configure Tauri CSP `frame-src`/remote-domain allowlist when building the real browser pane). Spike project + screenshots: `/tmp/tauri-spike/` (ephemeral).
- **The original "Tauri rejected" verdict (`tech-stack.md`) was under different assumptions.** It called WebKitGTK keyboard latency/IME/grab "fatal for a keyboard-first editor" — but that assumed a *custom web editor* carrying the full keyboard-first burden on WebKitGTK. Under the new architecture the editor is **nvim-in-xterm.js**, the same battle-tested input path VS Code uses for its terminal, which substantially de-risks that concern (and IME is a non-issue for Spanish/English). The verdict downgrades from "fatal" to "validate via spike," not "ignore."

## 10. Open questions (capture now, decide when building)

- **Git surface (lazygit-style):** own React git UI vs embedding a TUI (lazygit/gitui) in a terminal pane vs a hybrid. Terax shells out to the `git` binary (no libgit2) — a pragmatic reference.
- **Embedded browser depth:** iframe/web-preview (works for local dev servers + most pages; blocked by `X-Frame-Options`/CSP on some sites) vs a CDP-driven browser the agent fully controls (clicks/forms) displayed in-pane. Start with preview, grow to control.
- **How much of the Python backend ultimately becomes Rust** (Path-2 scope) — and when.

## 11. Reference: Terax (Apache-2.0)

`crynta/terax-ai` — Tauri 2 + Rust + React 19 + Vite + Tailwind/shadcn, xterm.js (WebGL) + `portable-pty`, CodeMirror 6 (vim-emulation — *not* embedded nvim), Vercel-AI-SDK BYOK agent (not the Claude SDK; no MCP), web-preview pane, OSC 7 + OSC 133 shell integration. **Apache-2.0** → forkable/liftable with attribution.

- **Lift candidates:** the `src-tauri/src/modules/pty/` layer (PTY + OSC 7/133 shell integration + DA filter) — more complete than our v1 terminal; the pane/stack layout system; the web-preview auto-detect.
- **Do NOT adopt:** its CodeMirror editor (opposite of our embedded-nvim transition); its Vercel-AI-SDK agent (we keep our claude-agent-sdk sidecar with `canUseTool`).
- **Caveat:** very young (created 2026-04), fast-moving — pin to a commit if vendoring.

## 12. Immediate next steps (agreed order)

1. **RAM baseline — DONE (2026-06-05).** Result in §9: Terax core 213 MB PSS vs Symmetria IDE 217 MB PSS — a wash. Footprint is *not* a pivot driver. Method: sum PSS over the process tree via `/proc/<pid>/smaps_rollup` (`/tmp/memtree.sh`), excluding the user's Claude session running inside Terax's terminal.
2. **WebKitGTK spike — DONE (2026-06-05), PASSED.** See §9: default rendering gives working transparency + blur + 60 fps + crisp fonts on this Optimus Hyprland laptop; the kill-switch workaround is unnecessary and actually harmful. Toolchain was already present (system `cargo` 1.95 + `webkit2gtk` 2.52) — Tauri stood up with zero installs. The one follow-up: verify remote-iframe loading + Tauri security config when the browser pane is built.
3. **Begin the file manager** ← NEXT — the first real deliverable (the IDE's file tree + git status derive from it). Build it as a standalone Tauri/React app first (cheap to fail, immediately useful), recreating the Symmetria look in CSS.
