# Identity

> **Framework pivot in progress (2026-06):** the IDE is moving from PySide6/QML to Tauri 2 (Rust + React/TS). Principles #2, #3, #5 below are updated to reflect it. See `docs/framework-pivot.md` for the full decision.

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
Text surfaces navigate with vim motions. Through the transition this is *real* NeoVim (the user's actual config); in the end-state web editor it is a vim-navigation layer (flash-like motions). The *feel* of vim navigation is sacred; the *engine* under it is swappable. (Was: "NeoVim motions are sacred" — softened by the framework pivot, see `docs/framework-pivot.md` §8.)

### 3. Aesthetic continuity with the Symmetria ecosystem
The IDE renders in **web (Tauri + React/TS)**; the Symmetria visual grammar shared with Shell and File Manager is **recreated in CSS**. The look is reproducible; the feel may differ slightly. (Was: "renders in QML" — the wrapper is moving to web.)

### 4. Compose, don't reimplement
NeoVim does editing. The IDE renders chrome. Claude Code does agent work. The IDE orchestrates — it does not replace what already works. *Reinforced* by the pivot: running NeoVim in a terminal (rather than a custom grid painter) is more faithful to this principle, not less.

### 5. Progressive extraction toward an own editor
NeoVim's responsibilities (status line, which-key, file tree, fuzzy finder, git) are peeled off one layer at a time into the IDE. The *wrapper* moves to Tauri/web as one decisive pivot; the *editor* is hollowed out progressively until NeoVim is "just a buffer," then swapped for an own web editor. The editing core stays real NeoVim until that end-state is reached.

### 6. Opinionated, not general-purpose
This is personal tooling. Decisions that serve the primary user are preferred over decisions that serve a hypothetical audience.
