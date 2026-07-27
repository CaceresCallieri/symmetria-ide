---
name: nested-compositor-clipboard
description: "Qt's nested Wayland compositor isolates the clipboard both ways; bridging it needs a ~30-line C++ QWaylandCompositor subclass (PySide6 has no bindings)"
metadata: 
  node_type: memory
  type: reference
  originSessionId: 1dc16f14-6524-437a-9b81-8d0fde68876c
  modified: 2026-07-27T04:33:31.275Z
---

# Nested-compositor clipboard is isolated both ways

Spiked live 2026-07-27 while scoping the in-window browser (real Chrome as a
`ShellSurfaceItem` inside the IDE — see [chrome-host-external-browser](../../project/active/chrome_external_browser.md)).

**Measured, not inferred.** A PySide6 app hosting `QtWayland.Compositor`, with a
Qt client running inside it (`WAYLAND_DISPLAY=<nested socket>`):

| direction | result |
|---|---|
| client inside takes the selection | compositor's own `QGuiApplication.clipboard()` **unchanged** |
| client inside takes the selection | Hyprland's clipboard (`wl-paste`) **unchanged** |
| host/Hyprland owns a selection | client inside reads **`''`** |
| client reads back its own selection | works (isolated universe is self-consistent) |

So an embedded browser would be a clipboard island: copy a URL in the page and
you could not paste it into the IDE terminal, an agent pane, or anywhere else.
For a browser whose whole point is feeding the rest of the workflow, that is a
blocker, not a nit.

## The bridge exists in C++ and is small

`/usr/include/qt6/QtWaylandCompositor/qwaylandcompositor.h` has exactly the two
halves needed:

- **client → host:** `setRetainedSelectionEnabled(true)` + the protected virtual
  `retainedSelectionReceived(QMimeData*)` — fires whenever a nested client takes
  the selection. Push it into the host `QClipboard`.
- **host → client:** `overrideSelection(const QMimeData*)` — pushes a selection
  INTO the nested clients. Drive it from `QClipboard::dataChanged`.

## Why this forces C++ (and why it's cheap anyway)

`retainedSelection` is a `Q_PROPERTY`, so QML can turn retention ON — but that
alone bridges nothing. The two calls that move data are a **protected virtual**
(not a signal, so QML cannot receive it) and a **plain method taking
`const QMimeData*`** (not `Q_INVOKABLE`, and `QMimeData` is not a QML type). And
**PySide6 ships no `QtWaylandCompositor` bindings** — confirmed against 6.11.1's
module list.

Conclusion: a small C++ QML plugin subclassing `QWaylandCompositor`, on the order
of 30 lines. That is a shape this project already maintains (the qmltermwidget
fork), and the user has already said C++ is acceptable. So the clipboard is a
**scoping fact, not a viability blocker** for the nested-compositor browser.

Related: [fork-changes-need-makepkg](./fork_changes_need_makepkg.md) — any such
plugin has to be packaged before launcher-launched IDEs can load it.
