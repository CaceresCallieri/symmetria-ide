---
name: QML typed function parameters reject default values in Qt 6.11
description: `function fn(x: string = ""): void {...}` parses fine in tests but the running QML engine throws `Type annotations are not supported (yet)` at the column of the `=`, cascading the component to "unavailable".
type: reference
---

In Qt 6.11's QML grammar, typed function parameters (`x: string`) and
default-value expressions (`= ""`) are mutually exclusive. Combining
them — `function fn(x: string = "")` — produces this error at
QQmlApplicationEngine::load() time:

```
qt.qml — file:///.../FileTreeView.qml:NNN: <col>: Type annotations are
not supported (yet).
```

The error column points at the `=`, not the type annotation itself.
Untyped parameters with defaults (`x = ""`) work. Typed parameters
without defaults (`x: string`) work. Only the *combination* fails.

**Symptom cascade.** Because the error fires at component-load time,
the affected QML file becomes "unavailable" — and so does every
ancestor that imports it. The user-visible chain looks like:

```
FileTreeView.qml:NNN: Type annotations are not supported (yet)
  → file://.../FileManager.qml: Type FileTreeView unavailable
  → file://.../Main.qml: Type FmUi.FileManager unavailable
  → "failed to load Main.qml" → IDE exits with returncode 1
```

A reader who has only seen the cascade tip (`Main.qml failed to
load`) needs to trace the chain to find the real culprit at the
deepest level. Reading qt.qml log lines top-to-bottom in stderr
(not bottom-up) gets you there fastest.

**Why this is easy to ship by accident.** The combo parses cleanly:
JS-flavored linters accept it, pyright accepts it (it sees a QML
file, not Python), the project's pytest suite passes (tests don't
spin up `QQmlApplicationEngine.load()`), and code review reads the
default as a clean ergonomic improvement to the typed signature.
The only path that reveals the bug is **launching the IDE against
the source-tree FM** (`SYMMETRIA_IDE_FM_QML_PATH` set). Launches that
hit the installed FM at `/usr/lib/qt6/qml/Symmetria/...` never
exercise the new signature, so the bug stays latent until someone
re-installs or runs the bench.

**Fix.** Drop the default. Either:

1. `function fn(x: string): void` — keep the type, lose the default.
   Callers that omit the arg pass `undefined` to the JS body;
   `if (x && x !== "")`-style guards handle that idiomatically.
2. `function fn(x = ""): void` — keep the default, lose the type.
   Untyped JS-style; loses the documentation value of the annotation.

Option 1 is usually what you want — typed signatures protect against
caller mistakes, and the `undefined`-on-omission path is easy to
test for explicitly. Option 2 is appropriate when callers genuinely
need the default value to be applied at call-site rather than
explicitly checked in the function body.

**Where this has bitten us.** The 2026-05-23 review of FM commit
`87d931a` (option-4 lazyExpand shipping) recommended adding a default
to `_destroyModel(m: var, path: string)` "to match its documented
optionality." The recommendation was followed in commit `fc509f2`,
and the IDE's source-tree-FM bench broke immediately — but the
*installed* FM at `/usr/lib/qt6/qml/Symmetria/...` still had the
older signature, so the user's normal launches kept working. The
discrepancy hid the bug until the next bench run. Now fixed back
to option 1 (typed param, no default) with a load-bearing regression
note in `FileTreeView.qml` at the function definition.

**See also:** [QML strict-property on QObject](qml_strict_property_qobject.md)
— a different but related "looks legal, runs silently broken" QML
pitfall the same `FileTreeView.qml` has burned us on.
