---
name: qml-property-must-exist-on-type
description: Before fixing something by assigning a QML property, verify that property exists on THAT type — QML resolves it at runtime and a miss is nearly silent
paths:
  - "qml/**/*.qml"
---

When the fix is "assign property X", **confirm X is declared on the type you are
assigning it to** before writing it. Not on a similar type, not on a type that
sounds like a base class — that one. Check the type's `Q_PROPERTY` list, or the
`.qmltypes` for its module.

If the API you need turns out to be a **method** rather than a property, it has
no QML form at all and the fix belongs in C++.

**Why:** QML resolves property assignments at run time, and a miss is nearly
silent — one `Cannot assign to non-existent property` line per session, in a log
that carries plenty of routine warnings. Worse, the exception aborts the
enclosing function, so everything after the assignment silently never runs.

That is not hypothetical here. `BrowserPane.qml` walked the surface tree
assigning `item.hoverEnabled = true` to give the nested browser pointer motion.
`hoverEnabled` is a `Q_PROPERTY` on `MouseArea` and three QtQuickTemplates2
types — and on none of the Wayland item classes. The assignment threw on the
walk's FIRST line, aborting it before the recursion and before the
`childrenChanged` connect, so the walk never ran for the surface or for any
popup. It shipped, was reviewed, and survived a live debugging pass, because an
inert walk is indistinguishable from a working one by reading it. The real API
was `setAcceptHoverEvents` — a C++ method, which is why it could never have
worked where it was written. Full account:
`.claude/memory/reference/qt-pyside/nested_compositor_pointer_input.md`.

The trap generalises past that case: Qt exposing a property on ONE widget type
makes it read as though it lives on a common base. `hoverEnabled` on `MouseArea`
is exactly that shape.

**How to apply:**

- Verify before writing, cheapest source first: the type's header
  (`grep -r 'Q_PROPERTY(.* <name>' /usr/include/qt6/`), its module's
  `.qmltypes`, or a runtime metaobject dump.
- A property that resolves on the base but not the leaf is still a miss —
  check the type actually being instantiated, which for our compositor items is
  the subclass in `native/symmetria-compositor/`.
- When it turns out to be a method, do not look for a QML workaround: put it in
  the C++ subclass. That is what `SymmetriaShellSurfaceItem` and
  `SymmetriaOutput` exist for, and three of their four responsibilities are
  exactly this situation.
- After adding an assignment to a non-obvious type, run the IDE once and grep
  its log for `Cannot assign to non-existent property`. One line is the entire
  symptom.
