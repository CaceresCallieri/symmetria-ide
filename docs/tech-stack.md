# Tech Stack

> **Framework pivot REVERSED (2026-06-07) — this decision is live again.** A Tauri 2 (Rust + React/TS) pivot was decided 2026-06-05 then reversed; the IDE stays on **PySide6 (Qt 6 + Python + QML)**, exactly as this file's original decision says. NeoVim runs as a TUI inside a forked `QMLTermWidget` (Konsole's VT engine), not xterm.js and not the deleted custom grid renderer. `docs/framework-pivot.md` is a superseded historical record of the considered-and-rejected pivot. The original "Tauri rejected" reasoning below stands again; gpui returns to being the far-future full-rewrite candidate.

## Decision

- **Primary:** PySide6 (Qt 6 + Python + QML). *(Shipped Phases 0–2.5; live and current after the 2026-06 Tauri pivot was reversed.)*
- **Agent backend:** a Node SDK sidecar (`@anthropic-ai/claude-agent-sdk`) driven from the Python backend.
- **gpui** remains the far-future full-rewrite candidate (Zed's engine, pursued only if leaving Qt/QML is ever justified) — see `docs/future.md` "gpui migration."

## Primary libraries

| Concern              | Library                                   | Notes |
|----------------------|-------------------------------------------|-------|
| UI framework         | PySide6 (Qt 6.7+)                         | First-party Qt Python bindings, LGPL, healthy. |
| Declarative UI       | QML                                       | Already used across the Symmetria ecosystem. |
| NeoVim RPC           | `pynvim`                                  | Canonical msgpack-RPC client. |
| Agent bridge (Python) | `subprocess` + `json` (stdlib)            | Spawns the Node sidecar (`node sidecar/dist/index.js`); JSONL on stdin/stdout. No third-party Python deps. |
| Agent bridge (sidecar runtime) | Node `>=20`                      | Required to run the sidecar. Arch ships `>=22` in `nodejs`. |
| Agent bridge (sidecar source) | TypeScript 5.6+                   | `strict` + `noUncheckedIndexedAccess` + `noImplicitOverride`. Bundled to ESM via esbuild. |
| Agent bridge (sidecar bundler) | esbuild ^0.24                     | One-step bundle of `src/index.ts` → `dist/index.js`. SDK marked `external` (native binary opt-deps resolve from `node_modules` at runtime). |
| Agent SDK            | `@anthropic-ai/claude-agent-sdk@0.2.119`  | Exact pin (no caret) for reproducibility. Drives `query()` programmatically with the `canUseTool` callback for in-pane approve/deny. Same SDK used by Zed's `claude-code-acp`, opencode, and the official VS Code extension. |
| Browser embed        | `QtWebEngine`                             | Chromium-based, shipped with Qt. |
| Live QML reload      | `pyside6_live_coding` (dev only)          | Fast iteration in Phase 0. |

### Retired from the stack

- **`ptyprocess` + `pyte`** — originally planned to drive Claude Code through a pty and reconstruct structure from ANSI-decorated frames. Dropped in favour of the stream-json pivot — every turn's structure was directly readable as JSONL events, eliminating the terminal-emulation surface entirely. `[project.optional-dependencies].terminal` was removed from `pyproject.toml` as part of that pivot.
- **`claude -p --output-format stream-json` (CLI subprocess scrape)** — used by the Phase 2 placeholder spike. Self-resolved permissions server-side and exposed no in-band approve/deny surface, so any tool-using turn would either auto-deny or stall. Replaced by the Node SDK sidecar (`@anthropic-ai/claude-agent-sdk`) whose `canUseTool` callback gives us the structured permission request as a typed async function. See `docs/phases.md` Phase 2 for the pivot rationale.

## Why PySide6

### 1. QML reuse is free
The Symmetria File Manager is already QML. Loading it as a child component inside the IDE window is trivial. Every other framework (Tauri, Electron, Rust-native) would require a rewrite.

### 2. Aesthetic continuity
Symmetria Shell (QuickShell) and File Manager are QML. The IDE shares the visual grammar without effort.

### 3. Velocity for exploration
Python backend + QML frontend gives hot-reload, no compile step, minimal boilerplate. Phase 0 is about discovery — the framework should not tax iteration speed.

### 4. Progressive hardening, not rewriting
If profiling exposes a bottleneck (large buffer rendering, high-FPS animation), a C++ widget can be dropped into the same Qt app. The app shell does not change language — only the hot component does.

### 5. Escape hatch preserved
The long-term target is gpui (Rust). Until gpui is stable with a public API, waiting is correct. When the migration happens, Phases 0–4 in PySide6 will have taught us exactly what we want.

## Alternatives considered

### Qt in pure C++
- **For:** maximum performance, same widget tree.
- **Against:** slower iteration during exploration; compile step taxes Phase 0 discovery.
- **Verdict:** start in Python. Port hot paths to C++ later, inside the same Qt app.

### Tauri (Rust + Web frontend) — **considered 2026-06, RE-REJECTED (2026-06-07)**
- **For:** lightweight bundle (~7-10 MB vs Electron's 150+), aligned with long-term Rust direction, *far* better AI-codegen + library ecosystem than QML, native embedded browser, agent-frontend ecosystem to lean on (Terax + web Claude UIs).
- **Original against (reconsidered during the pivot, now moot):** "WebKitGTK on Linux has input-latency, IME, and keyboard-grab issues — fatal for a keyboard-first editor." The pivot argued this assumed a *custom web editor* carrying the full keyboard-first burden, whereas its architecture *would have* run **NeoVim in xterm.js** (the input path VS Code uses for its terminal), de-risking latency/grab; IME is a non-issue for Spanish/English. With the pivot reversed, this reconsideration is moot — the concern never had to be tested.
- **Residual risks (validated):** WebKitGTK runtime RAM is NOT lighter than Qt (measured: Terax 213 MB ≈ Symmetria IDE 217 MB — a wash); Wayland/Hyprland rendering on this NVIDIA-Optimus laptop — **spiked 2026-06-05, PASSED** (transparency + blur + 60fps, no black box; DMABUF kill-switch harmful). See `docs/framework-pivot.md` §9.
- **Current verdict: NOT ADOPTED.** Tauri was adopted 2026-06-05 then reversed 2026-06-07 — the decisive factor: file-tree + git-status are reused across Shell + FM + IDE via the FM's `Symmetria.FileManager.UI` QML module, which a web IDE can't embed without reimplementing (a DRY violation). The executed Tauri work (themed shell + PTY terminal + nvim-in-xterm.js) never shipped to `main` and is archived at `git tag archive/tauri-pivot`. The for/against analysis above is retained as the rationale trail; the original PySide6/QML decision stands.

### Electron
- **For:** huge ecosystem, easy browser embed.
- **Against:** 150–250 MB idle baseline even in Electron 34 (2026). Aesthetic drift toward generic web look. Symmetria aesthetic must be re-created in CSS.
- **Verdict:** rejected. Contradicts *"beauty in functionality."*

### Native Rust (iced / egui / gpui)
- **For:** ultimate performance ceiling.
- **Against:** gpui is pre-1.0 with frequent breaking changes; iced/egui are immediate-mode and awkward for retained-mode IDE UI; building NeoVim embed + pty + file manager + browser from scratch in Rust is months before the first pixel.
- **Verdict:** rejected for now; revisit for the long-term rewrite.

### Slint
- **Against:** the project's own Oct 2025 "Making Slint Desktop-Ready" post acknowledges desktop maturity still lags embedded.
- **Verdict:** not ready in 2026.

### Dioxus
- **Against:** WebView-based (WebKitGTK on Linux) — same IME/latency issues as Tauri. Native Blitz renderer still experimental.
- **Verdict:** rejected.

### Flutter desktop
- **Against:** weak Linux pty story; Symmetria aesthetic would require heavy custom widgets (hostile DX).
- **Verdict:** rejected.

## Design pitfalls to avoid (goneovim autopsy)

`goneovim` (Go + Qt) was a working Neovim-embed proof point but is archived in 2024 — its author moved to a Zig project. The lessons:

1. **Never pair Qt with a third-party GC-language binding.** goneovim used `therecipe/qt`, which went unmaintained. PySide6 is first-party Qt Company and safe.
2. **Use TTF fonts, not OTF** — goneovim's OTF rendering broke.
3. **Ligatures are a performance hazard** — gate behind a flag if supported at all.
4. **CJK IME has crashed historical implementations** — handle carefully (non-issue for Spanish/English primary use).
5. **`ext_popupmenu` / `ext_cmdline` cost FPS if overused** — use deliberately for extraction targets, not as default hooks.

## Reference codebases (to read, not depend on)

- `equalsraf/neovim-qt` — C++/Qt Neovim embedding, still maintained. Proof that Qt + Neovim is viable.
- `neovide` — Rust + Skia + winit + msgpack-RPC. The cleanest `redraw`-event renderer to study before the long-term gpui rewrite.
- `cmux` — Swift + libghostty + WebKit. macOS-only, but its socket-controlled agent-pane architecture is the reference pattern for Phase 4.
