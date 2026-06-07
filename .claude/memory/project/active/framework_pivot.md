---
name: framework-pivot-pyside6-qml-tauri-2-rust-react-ts
description: REVERSED 2026-06-07. The Tauri/web pivot was decided 2026-06-05, partly executed, then abandoned. The IDE stays native PySide6/QML. Plan QML work, not web. Detail in CLAUDE.md banner + docs/framework-pivot.md (superseded record).
metadata: 
  node_type: memory
  type: project
  originSessionId: 24d3d78f-ecfb-4678-b251-3411e2a2621a
---

**DECISION REVERSED (2026-06-07): the IDE stays on PySide6 (Qt6 + Python + QML).** A Tauri 2 (Rust + React/TS) pivot was decided 2026-06-05 and partly executed (themed shell + PTY terminal + nvim-in-xterm.js editor on branch `tauri-pivot`), then abandoned. **Do NOT plan or build Tauri/web work.** Target the live QML stack.

**Why reversed (the load-bearing reason):** the file-tree + git-status systems are **modularized and reused across Symmetria Shell + File Manager + IDE**. The FM exposes them as the `Symmetria.FileManager.UI` QML module (imported as `import Symmetria.FileManager.UI as FmUi`; its `Theme` singleton renamed `FmTheme` to avoid collision). Reuse requires **one shared toolkit**; the FM — after its own deep evaluation (GPUI ruled out on Hyprland: window-map failure #37918 + compositor-CPU defect; Slint viable & embeddable but QML still felt smoother; FM already feature-complete) — stayed native Qt/QML. A web IDE could not embed the QML module without reimplementing it (a DRY violation the user explicitly rejected: "we should modularize and reuse these systems"). **Secondary factor:** the WebKitGTK scroll/animation perf regret the FM measured live. The §9 caveats (RAM wash, favorable WebKitGTK spike on this Optimus laptop) remain factually true — they just no longer outweigh the reuse coupling.

**Where the abandoned work lives:** `git tag archive/tauri-pivot` (tip `414f8d7`). Recover with `git switch -c tauri-pivot archive/tauri-pivot`. NOT in the working tree (the 5.4 GB `app-tauri/` build artifacts were removed; `main` never tracked any Tauri source).

**Salvaged to main (the one pivot-independent win):** `src/symmetria_ide/jsonl_transport.py` — consolidated JSONL framing extracted from `session_host.py` + `term_repl.py` (commit `11645c0`, behavior-preserving, 932 tests green).

**How to apply:** Build/plan IDE features on the **live QML/PySide6 architecture** — the shipped code (Phases 0–2.5) is the implementation to extend, not a spec to re-deliver in web. The CLAUDE.md gotchas about QML/PySide/the grid renderer/which-key Lua are **current constraints again**, not retired history. For file-tree/git-status, reuse the FM's `Symmetria.FileManager.UI` QML module rather than reimplementing (user says FM reuse is "already in place"). See [ide_owns_keybind_layer](../meta/ide_owns_keybind_layer.md) — the which-key precedence contract still holds, now as a QML overlay (not an IDE-level web gate). Decision brief: `/home/jc/.claude/relay/20260607-183500-symmetria-ide-framework-decision.md`.
