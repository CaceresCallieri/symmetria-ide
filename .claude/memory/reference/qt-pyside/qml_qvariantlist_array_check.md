---
name: QML Array.isArray returns false for QVariantList
description: PySide6 marshals Python `list` to a QML-side `QVariantList` that is array-LIKE but does NOT pass `Array.isArray()`. Use duck-typed `x != null && x.length > 0` instead.
type: reference
---

In Qt 6.11, `Array.isArray(value)` is `false` whenever `value` came
from the Python side via `@Property(list, notify=...)` — PySide6
hands QML a `QVariantList`, which behaves like an Array (has
`length`, integer indexing, is iterable) but isn't actually a JS
Array as far as the `Array` constructor is concerned.

A QML entry gate of the shape:

```qml
if (Array.isArray(prop) && prop.length > 0) { ... }
```

will reject every Python-supplied list, regardless of content.
The fix is duck-typing:

```qml
if (prop != null && prop.length > 0) { ... }
```

This works for both true JS Array literals (the FM may build one
internally) and PySide6-supplied QVariantList values.

**Where this has bitten us.** Option 6's `restoreExpandedPaths` prop
on `FileTreeView.qml`: the IDE's `AppController.expandedPathsCache`
property exposed a Python `list[str]`, the QML binding evaluated
fine and the prop held the right values, but `Array.isArray()` at
the entry gate inside `onRootPathChanged` rejected every restore
attempt. Symptom: the FM kept running the lazyExpand cascade
regardless of cache contents; `tree mount settled` log lines read
`(lazy: N dirs)` instead of `(lazy: 0 dirs)`. Diagnosed by adding
a temporary `Logger.info` inside the entry gate and observing the
rejection.

**Why this is easy to ship by accident.** `Array.isArray` is the
canonical "is this a JS array" check in modern JS — pyright and
qmllint accept it, no warnings fire, the prop reads as `[a, b, c]`
in any debugger. Only the runtime gate fails, silently, against
QVariantList. Pyright doesn't model the QML/Qt boundary, so it
can't catch this class of bug.

**Diagnostic shortcut.** When a QML `if (Array.isArray(...))` guard
appears to be rejecting Python-supplied data, log `typeof prop`
and `prop.constructor.name` — for a QVariantList you'll see
`object` and `QQmlListWrapper` or similar (NOT `Array`).
Confirmation that the duck-typed form will work.

**See also:**
- [QML strict-property on QObject](qml_strict_property_qobject.md) — a
  similar "looks legal, runs silently broken" QML pitfall around
  the JS/QObject boundary.
- [QML typed param + default value](qml_typed_param_no_default.md) —
  same theme: Qt 6.11 grammar/runtime restrictions that pass
  static analysis.
