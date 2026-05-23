---
name: QML strict-property mode rejects dynamic-property assignment on C++ QObjects
description: Under `pragma ComponentBehavior: Bound`, `obj.someProp = fn` on a C++ QObject silently fails with a Qt log warning; use a JS-side map keyed by stable identity instead.
type: reference
---

Under `pragma ComponentBehavior: Bound` (set at the top of
`FileTreeView.qml` and any other QML component that opts into the
strict-binding regime), QML rejects assignment to **non-declared**
properties on C++ QObject-derived types. Concretely:

```qml
// inside FileTreeView.qml, with pragma ComponentBehavior: Bound at the top
const m = fsModelComponent.createObject(root, { path: path, ... });
m._scheduleOnChange = scheduleOnChange;   // silently fails
```

**Symptom.** The assignment emits a single Qt-log warning of the form
`qt.qml — file:///…/FileTreeView.qml:NNN: Error: Cannot assign to
non-existent property "_scheduleOnChange"` and otherwise no-ops. Code
that depends on reading the value back (e.g. `if (m._scheduleOnChange)
m.entriesChanged.disconnect(m._scheduleOnChange)`) silently skips the
branch, because the property never landed. Trees still mount because
the second connect line on `m.entriesChanged` works on its own — but
nothing can ever disconnect the handler later, and future readers
diagnosing the warning conclude the disconnect machinery is broken.

**Why it survived /seal in commit 74b7c6f.** The pattern looks like
ordinary JavaScript and runs without throwing. The code-reviewer agent
sees a connect/disconnect pair and reads it as correct. The bench's
"tree mount settled" line still emitted because the FIRST mount works
(connect succeeds; disconnect is only exercised on `_resetTreeState` /
`_collapse`). And the installed FM under `/usr/lib/qt6/qml/...`
predated the commit, so the user-visible IDE flow (no
`SYMMETRIA_IDE_FM_QML_PATH` override) never hit the bug.

**Fix shipped** (`feat(file-tree): lazyExpand prop + ...`, 2026-05-23):
store the handler in a path-keyed JS map declared as a regular `var`
property on the QML root — `_modelHandlers: ({})` — and look it up via
`_modelHandlers[path]` inside `_destroyModel`. Path keys are stable
because there is exactly one model per directory at a time
(`_models[path]` is the registration map; the orphan-race branch in
the `_expand` finish callback is dead code per its own docstring).

**General rule for any future QML code touching QObject instances.**
The only places you can attach state to a C++ QObject from QML are:

1. **Declared properties on the C++ side** — would need a sub-class.
   Out of reach for us (FileSystemModel is provided by an external
   library).
2. **Attached properties** — only when the C++ class exposes a Q_OBJECT
   with the `QML_ATTACHED` machinery. Most don't.
3. **A parallel JS map on the QML root** — the option we chose.
   Cheap, immediate, no C++ changes. Use a stable JS-comparable key
   (string path, integer id) since JS object identity comparison is
   reference-based and reference equality on shiboken-wrapped QObjects
   can drift between calls.

Avoid `WeakMap` keyed by the QObject directly — PySide6 wraps a QObject
in a Python shiboken proxy whose identity is not guaranteed stable
across QML traversals, and QML's JS engine may produce a different
wrapper on different access paths even when the underlying C++ pointer
is the same. The path-keyed map sidesteps this entirely.

**Cost paid.** Two `Object.assign({}, _modelHandlers)` allocations per
mount per directory (create + delete on collapse) — utterly negligible
vs. the IO cost of the actual scan. The map's lifetime tracks
`_models` exactly: cleared in `_resetTreeState`, mutated in lockstep
with `_expand` / `_destroyModel`.

**See also:** [QML overlay focus](qml_overlay_focus.md) — another
non-obvious binding/lifecycle pitfall in this codebase's QML layer.
