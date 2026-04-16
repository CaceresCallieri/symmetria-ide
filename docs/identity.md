# Identity

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

### 2. NeoVim motions are sacred
Text surfaces navigate with NeoVim motions. No deviation, no "just this once" exceptions.

### 3. Aesthetic continuity with the Symmetria ecosystem
The IDE renders in QML to share a visual grammar with Symmetria Shell and Symmetria File Manager.

### 4. Compose, don't reimplement
NeoVim does editing. Qt does rendering. Claude Code does agent work. The IDE orchestrates — it does not replace what already works.

### 5. Progressive extraction, not big-bang rewrite
NeoVim's UI chrome (status line, command line, fuzzy finder) is peeled off one layer at a time into native UI. The editor core stays untouched for years.

### 6. Opinionated, not general-purpose
This is personal tooling. Decisions that serve the primary user are preferred over decisions that serve a hypothetical audience.
