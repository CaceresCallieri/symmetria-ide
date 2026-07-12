---
name: shift-s-back-navigation
description: "Shift+S is the project-wide \"go back\" key for drill-in views inside a central surface"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 7cb5872e-ccc5-40ea-941a-f0a0567ee3e9
---

Shift+S (capital S) is the canonical **"go back"** key for drill-in navigation inside the IDE's central surfaces — any view that replaces its parent view in place (e.g. the PR conversation replacing the PR list in the git surface's "prs" tab, decided 2026-07-12).

**Why:** Drill-in views need one predictable, surface-independent back gesture the user can rely on without remembering per-view bindings. `Esc`/`h` stay available as vim-flavored aliases where they don't conflict, but they're overloaded elsewhere (Esc doubles as toast-dismiss in list leaves; h is a tree-collapse/left motion in FM-based views), so Shift+S is the one binding guaranteed to mean "back" everywhere.

**How to apply:** When building any new drill-in (a detail view shown in place of its list, a nested menu level, etc.), bind `Qt.Key_S` with `Qt.ShiftModifier` → the back/close intent signal, alongside any view-local aliases. Keep bare `s` free. Handle keys at the drill-in's root item (not an inner conditionally-visible child) so back works during loading/error states too — see PrDetailView.qml's focusList note for the reference implementation.
