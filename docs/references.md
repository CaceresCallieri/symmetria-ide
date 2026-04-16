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
