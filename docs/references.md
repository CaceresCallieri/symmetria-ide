# References

Projects that inform this one. Listed with *what we take* from each.

## Zed

https://zed.dev

Rust-based editor built on the custom `gpui` framework — GPU-accelerated, hybrid immediate+retained-mode, 120 FPS target, ~10× faster cold start than VS Code.

- **What we take:** aesthetic north star, performance mental model, long-term migration target (gpui).
- **What we do not take:** Zed's opinionated editor behavior. We keep NeoVim.

## Warp

https://www.warp.dev

Rust + GPU terminal. Block-based data model — every command is a block with input/output/metadata, populated via shell hooks (`precmd` / `preexec`) emitting DCS sequences.

- **What we take:** the block-based rendering model for the agent pane in Phase 2.

## cmux

https://github.com/manaflow-ai/cmux

Native macOS coding-agent multiplexer. Swift + AppKit, `libghostty` for terminal rendering, WebKit for embedded browser, Unix-domain-socket + JSON for agent IPC. Scriptable browser API (accessibility-tree snapshot, element refs, `click`/`fill`/`evalJS`).

- **What we take:** the architecture pattern — a socket-controlled pane manager with scriptable embedded browsers the agent can drive.
- **What we cannot take:** the code (macOS-only).

## Neovide

https://github.com/neovide/neovide

Rust + Skia + winit + msgpack-RPC Neovim GUI.

- **What we take:** the reference implementation for rendering NeoVim `redraw` events. Essential reading before the long-term gpui rewrite.

## goneovim

https://github.com/akiyosi/goneovim

Go + Qt Neovim GUI. **Archived in 2024** — author moved to a Zig project.

- **What we take:** the pitfall catalog (OTF fonts, ligatures, IME, multi-DPI font sharpness, `ext_popupmenu` perf costs). Also the cautionary lesson: never pair Qt with a third-party GC-language binding that can go unmaintained.

## equalsraf/neovim-qt

https://github.com/equalsraf/neovim-qt

C++/Qt Neovim GUI. Still maintained.

- **What we take:** proof that Qt + NeoVim embed remains a viable, living path.

## orchestrator.nvim

Personal NeoVim plugin for driving the Claude Code workflow. Source of the capsule model surfaced in Phase 0's status bar.

- **What we take:** the capsule concept — small, composable state indicators that the native status bar will render.

## Emacs

The spiritual reference — *"everything inside one coherent environment"* — modernized, aesthetic, and agent-native in this project's interpretation.

## Terax (terax-ai)

https://github.com/crynta/terax-ai

Tauri 2 + Rust + React 19 "AI-native terminal" (their term: ADE — *AI Development Environment*). ~7 MB bundle. Pairs a native PTY backend (`portable-pty`) with xterm.js + WebGL rendering, a CodeMirror 6 editor with vim mode, a file explorer, a web-preview pane that auto-detects local dev servers, and a BYOK AI side-panel supporting multiple providers + local models via LM Studio. Project memory file (`TERAX.md`) mirrors the `CLAUDE.md` convention. Shell integration injects init scripts that emit cwd / prompt markers (same approach Warp uses).

- **What we take:**
  - **Validation of the integrated four-pane shape** (terminal + editor + file tree + AI) — same target as ours, shipped and lightweight, evidence the form factor works without the bloat of an Electron IDE.
  - **`portable-pty` as the concrete PTY crate** for the eventual gpui rewrite — Rust, cross-platform, already battle-tested in Terax + Wezterm.
  - **Web-preview auto-detection of dev servers** — strong candidate for a Phase 3+ feature once the terminal pane lands; we can detect port-binding from process scans or shell-emitted markers.
  - **Shell-integration via OSC sequences from injected `precmd`/`preexec` hooks** — the canonical way to report cwd and prompt boundaries; we'll need this for the "file tree follows cwd until anchored" idea.
- **What we evaluate, not adopt blindly:** Tauri 2 as a *third* possible future-target frontend stack alongside gpui. If we ever decide the Qt → gpui migration is too heavy, Tauri is the alternative worth costing out (WebView2/WebKit shell, Rust backend, mature plugin ecosystem). Caveat: WebKitGTK on Wayland still has the rendering-glitch + DMABUF caveat Terax's own README documents — relevant for our Hyprland-first audience.
- **What we do not take:** xterm.js. Tying our terminal to a JS canvas widget would commit us to a WebView in every frontend stack, foreclosing both gpui and pure-Qt paths.
