---
name: framework-pivot-pyside6-qml-tauri-2-rust-react-ts
description: Decided 2026-06-05. The wrapper moves to Tauri/web; NeoVim runs in xterm.js (no custom grid renderer); which-key becomes an IDE-level gate; long arc replaces nvim with an own web editor. Full detail in docs/framework-pivot.md.
metadata: 
  node_type: memory
  type: project
  originSessionId: 24d3d78f-ecfb-4678-b251-3411e2a2621a
---

**Decision (2026-06-05):** Symmetria IDE's *wrapper* pivots from PySide6 (Qt6 + Python + QML) to **Tauri 2 (Rust core + React/TS frontend)**, modeled on Terax (`crynta/terax-ai`, Apache-2.0). **Authoritative doc: `docs/framework-pivot.md`** — read it before any pivot work; this memory is just the index pointer + the load-bearing decisions.

**The non-obvious decisions (so future sessions don't re-litigate or mis-build):**

- **NeoVim renderer is DROPPED, not ported.** No custom `ext_linegrid` grid painter, no scroll/cursor animations. NeoVim runs as a TUI inside **xterm.js** (same terminal tech as shell terminals). The user explicitly accepted losing the animations. Porting Neovide-grade animation to web canvas is the one no-living-precedent task — we decline it on purpose.
- **Chrome survives via `nvim --listen`.** A second RPC connection (alongside the PTY) keeps the capsule emitter / OSC-7 / git integration driving native web side panels. nvim supports concurrent connections.
- **which-key is elevated to an IDE-level command gate** (see [ide_owns_keybind_layer](../meta/ide_owns_keybind_layer.md) for the full precedence contract). Key point: it owns the keymap and delegates unclaimed keys to nvim; `<leader>` is "born in B" (merges nvim's leader subtree day one) or nvim's leader namespace goes dead. The custom Lua which-key (CLAUDE.md #15–#21) is *deleted*, not ported.
- **Backend: Python sidecar now → Rust later.** Keep the Python backend as a sidecar the Tauri shell talks to (fastest to a running IDE); collapse to a Rust core (nvim-rs + portable-pty, lift Terax's PTY layer) once proven. The Node agent SDK sidecar is already TS and carries over unchanged.
- **Long arc = identity shift.** Progressively strip nvim (file tree → file searcher → lazygit → orchestrator) until it's "just a buffer," then replace with an own web editor (Monaco/CodeMirror) + vim-nav (flash). Non-negotiable #3 softens from "NeoVim motions sacred" to "vim-style navigation preserved."

**Why (drivers):** dev velocity + AI codegen (TS/React ≫ QML — Qt itself admits QML is a weak codegen target); agent-frontend ecosystem to lean on; **native embedded browser** for agent web work (free in Tauri, heavy QtWebEngine in Qt); web library ecosystem + Terax to lean on.

**Honest caveats (don't bank on the wrong premise):**
- **RAM is NOT a pivot driver — MEASURED 2026-06-05:** Terax core 213 MB PSS ≈ Symmetria IDE idle 217 MB PSS (within 2%; IDE understated, its sidecar didn't pre-warm). The "we're much heavier than Terax" premise is false. Each stack has a fat baseline (WebKitGTK WebView 161 MB vs Qt+Python 178 MB); the pivot is RAM-neutral-to-slightly-worse. Only real lever = drop Python → Rust core, and WebKitGTK's ~160 MB WebView still stays. Justify the pivot on dev-velocity/AI-codegen/agent-ecosystem/embedded-browser/bundle-size, NOT RAM.
- WebKitGTK on this **NVIDIA-Optimus hybrid** Hyprland laptop — **SPIKED 2026-06-05, PASSED.** Default rendering: transparency + backdrop-blur + 60fps canvas + crisp fonts all work (no black-box failure). The `WEBKIT_DISABLE_DMABUF_RENDERER`/`COMPOSITING_MODE` workaround is unnecessary AND harmful here (breaks blur → opaque panels) — do NOT use it. Toolchain was already installed (system cargo + webkit2gtk 2.52). Unverified: PRIME-offload / external-monitor-on-NVIDIA; remote-iframe load (data: URL rendered; external load inconclusive — configure Tauri CSP frame-src/remote-domain allowlist for the browser pane).

**Roadmap:** file manager (file tree + git status derive from it) → agent panel + browser → git system → which-key gate → web editor pane (last).

**Immediate next steps (agreed order):** (1) RAM baseline — DONE (2026-06-05); (2) WebKitGTK spike — DONE, PASSED (2026-06-05); (3) begin the Tauri file manager ← NEXT.

**How to apply:** When asked to build or plan IDE features, target the Tauri/web architecture, not QML. Treat the shipped QML code (Phases 0–2.5) as the behavior spec to re-deliver, not the implementation to extend. The CLAUDE.md gotchas about QML/PySide/the grid renderer/which-key Lua describe the *retired* stack — relevant as history, not as current constraints.

