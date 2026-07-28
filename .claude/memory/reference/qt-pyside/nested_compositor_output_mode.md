---
name: nested-compositor-output-mode
description: "The nested wl_output must describe the PANE, not the host window; only C++ can express it"
metadata: 
  node_type: memory
  type: reference
  originSessionId: 1dc16f14-6524-437a-9b81-8d0fde68876c
  modified: 2026-07-28T02:59:28.621Z
---

The nested browser's `wl_output` is the "screen" Chrome believes it is on, and
it must describe the **pane**, not the IDE window. Live code:
`native/symmetria-compositor/symmetriaoutput.{h,cpp}` and the
`SymmetriaOutput { ... }` block in `qml/browser/BrowserPane.qml`, both of which
carry the argument inline. This file is the derivation and the rejected routes.

## Why the output is the only lever

Chrome decides for ITSELF whether a dropdown fits below the omnibox, using the
screen it was advertised. Qt does not second-guess it: `XdgPopupIntegration`
places every popup at `unconstrainedPosition` under a literal
`//TODO check positioner constraints etc... sliding, flipping`. So the output
description is not one input among several — it is the whole of our influence
over popup placement.

## What was wrong, measured

The output tracked the host WINDOW (Qt's `sizeFollowsWindow`) while toplevels
were configured to the pane, in a unit basis 0.8x ours (the mode used the host
DPR 1.6 while we advertise scale 2). Measured live: a **1311x868 Chrome window
on a 1273x733 screen** — a window 135px taller than its own screen, so anything
anchored low had nowhere to go and Chrome flipped the omnibox dropdown up over
the omnibox. The two errors cancel for apparent content size, which is why it
looked fine.

## Why it needs C++

Stock `WaylandOutput` cannot express a size at all: `geometry` is READ-only (no
WRITE in the Q_PROPERTY), `setCurrentMode` is not `Q_INVOKABLE`, and
`sizeFollowsWindow` tracks the whole window. `QWaylandQuickOutput` (the type QML
instantiates) adds only `automaticFrameCallback`.

**`availableGeometry` looks like the QML-only answer and is a dead end**: there
is no available-geometry request in the `wl_output` protocol, so Qt uses it
purely internally (placing its own maximized windows) and it never reaches the
client. Do not re-try this route.

## Invariants that are easy to break

- **`sizeFollowsWindow: false` is mandatory** — otherwise Qt overwrites the
  mode on the next window resize. `setModeSize` warns once if it is left on,
  because the only other symptom is a subtly misplaced popup.
- **The mode must be `pane × scaleFactor`** while toplevels are configured in
  RAW pane units. The client divides the mode by the advertised scale, so both
  land on the pane and window == screen. Two halves in different files with no
  runtime check between them.
- **`addMode` before `setCurrentMode`** — `setCurrentMode` looks the mode up in
  the list and, per Qt, warns "Cannot set an unknown QWaylandOutput mode as
  current" and leaves the output unchanged. Reversed, every push is a silent
  no-op.
- **With `sizeFollowsWindow` off, Qt's `initialize()` adds NO mode**, so the
  output starts with no resolution and the first push must not wait on a timer.
- **`addMode` APPENDS and every change re-broadcasts the whole list** (Qt's
  in-place mode setter is private), so pushes are settle-debounced — EXCEPT on
  growth. While the pane grows, the toplevel is configured per frame and a
  lagging mode reinstates exactly the window-bigger-than-screen state above,
  for the length of the drag.
- **QML's `size` is a QSizeF and the metacall boundary truncates**, so callers
  round rather than pass a fractional product.

Related: [nested compositor pointer input](./nested_compositor_pointer_input.md).
