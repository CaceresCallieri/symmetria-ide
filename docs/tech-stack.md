# Tech Stack

## Decision

- **Primary:** PySide6 (Qt 6 + Python + QML).
- **Long-term migration target:** gpui (Zed's engine), once it has a stable public API — likely 2027+.

## Primary libraries

| Concern              | Library                          | Notes |
|----------------------|----------------------------------|-------|
| UI framework         | PySide6 (Qt 6.7+)                | First-party Qt Python bindings, LGPL, healthy. |
| Declarative UI       | QML                              | Already used across the Symmetria ecosystem. |
| NeoVim RPC           | `pynvim`                         | Canonical msgpack-RPC client. |
| pty spawning         | `ptyprocess`                     | Spawns Claude Code and shells. |
| Terminal emulation   | `pyte`                           | Python VT emulator — becomes the terminal backend. |
| Browser embed        | `QtWebEngine`                    | Chromium-based, shipped with Qt. |
| Live QML reload      | `pyside6_live_coding` (dev only) | Fast iteration in Phase 0. |

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

### Tauri (Rust + Web frontend)
- **For:** lightweight, aligned with long-term Rust direction (~10 MB vs Electron's 150+).
- **Against:** WebKitGTK on Linux has input-latency, IME, and keyboard-grab issues — **fatal** for a keyboard-first editor. QML File Manager would require full rewrite in HTML/CSS.
- **Verdict:** rejected.

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
