# Identity

> **Framework pivot REVERSED (2026-06-07):** a Tauri 2 (Rust + React/TS) pivot was decided 2026-06-05 then reversed — the IDE stays on **PySide6 (Qt 6 + Python + QML)**. The principles below are the live QML baseline; `docs/framework-pivot.md` is now a superseded historical record.

## Name

**Symmetria IDE** — part of the Symmetria family (Shell, File Manager, WhatsApp, and now IDE).

The name *Symmetria* sits in a conceptual cluster with sister projects:

| Name       | Meaning                         |
|------------|---------------------------------|
| Kosmos     | order emerging from chaos       |
| Symmetria  | harmony through proportion      |
| Vigilia    | readiness and presence          |

These are not product brands. They are *states of systems thinking* — each project embodies one of them.

## Tagline

> *The beauty in functionality and the functionality of beauty.*

This is a design constraint, not a slogan. Every decision is tested against both faces:

- **Beauty in functionality** — the tool must *feel* good to use. Responsive, minimal, calm.
- **Functionality of beauty** — aesthetic choices must earn their place. Decoration without purpose is rejected.

## Design principles

### 1. Keyboard-first
No interaction requires a mouse. Keyboard pathways are primary; mouse is secondary and optional.

### 2. Vim-style navigation is preserved
Text surfaces navigate with vim motions. Through the transition this is *real* NeoVim (the user's actual config); in a future own-editor end-state the feel is preserved via a vim-navigation layer (flash-like motions). The *feel* of vim navigation is sacred; the *engine* under it is swappable — and whether that future editor is web or native is unsettled post-reversal. (Was: "NeoVim motions are sacred" — softened because the engine is swappable; see `docs/future.md` "Own editor core.")

### 3. Aesthetic continuity with the Symmetria ecosystem
The IDE renders in **native QML** bound to the `Theme` design-token singleton; the Symmetria visual grammar shared with Shell and File Manager comes through directly — the file tree + git status are the FM's `Symmetria.FileManager.UI` QML module, reused rather than reimplemented.

### 4. Compose, don't reimplement
NeoVim does editing. The IDE renders chrome. Claude Code does agent work. The IDE orchestrates — it does not replace what already works. Running NeoVim as a TUI inside a terminal widget (rather than a custom grid painter) is more faithful to this principle, not less.

### 5. Progressive extraction toward an own editor
NeoVim's responsibilities (status line, which-key, file tree, fuzzy finder, git) are peeled off one layer at a time into the IDE. The *wrapper* stays native QML; the *editor* is hollowed out progressively until NeoVim is "just a buffer," then eventually swapped for an own editor core. The editing core stays real NeoVim until that end-state is reached; whether the replacement is web or native is unsettled post-reversal.

### 6. Opinionated, not general-purpose
This is personal tooling. Decisions that serve the primary user are preferred over decisions that serve a hypothetical audience.
