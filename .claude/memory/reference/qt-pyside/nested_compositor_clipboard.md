---
name: nested-compositor-clipboard
description: "Nested clipboard is isolated both ways; the C++ bridge SHIPPED 2026-08-04 — and the two directions have DIFFERENT focus requirements"
metadata: 
  node_type: memory
  type: reference
  originSessionId: 1dc16f14-6524-437a-9b81-8d0fde68876c
  modified: 2026-08-04T06:26:45.838Z
---

# Nested-compositor clipboard is isolated both ways

> **STATUS: SHIPPED 2026-08-04** as `SymmetriaCompositor` in
> `native/symmetria-compositor/`, and verified in both directions — see the
> section at the end. **Everything between here and there is the pre-build
> scoping record**, kept for the reasoning trail; it is written in the
> speculative tense of a decision not yet made.

Spiked live 2026-07-27 while scoping the in-window browser (real Chrome as a
`ShellSurfaceItem` inside the IDE — see [agentic-browser-state](../../project/active/agentic_browser_state.md)).

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

## SHIPPED and verified end to end (2026-08-04)

Built as `SymmetriaCompositor` in `native/symmetria-compositor/`. Round-tripped
sentinel strings both ways, exact match, with the plugin's
`SYMMETRIA_COMPOSITOR_DEBUG=1` trace confirming each hop.

**The two directions have DIFFERENT focus requirements, and that asymmetry is
the whole of what is surprising here:**

| direction | needs the IDE focused? |
|---|---|
| nested → host (`writeText` → `wl-paste`) | **no** — worked with the IDE unfocused on another workspace |
| host → nested (`wl-copy` → `readText`) | **yes**, at some point after the copy |

nested → host is free because the client only needs focus in OUR seat, which the
nested compositor grants regardless of the host. host → nested is gated because
an unfocused Qt app receives no data offer, so `QClipboard::mimeData()` is empty
and `pushHostSelectionToClients` returns before its trace ever prints.

**The stale read that results is NOT a bug and NOT the loop guard.** Measured:
copy on the host with the IDE unfocused, and the client keeps reading its
previous value at 0s, 2s and 5s — no trace fires at all. Focus the IDE and the
trace fires immediately and the client reads the current value. It is a latency
property with a self-healing edge. Do not "fix" it by polling the host
clipboard: an unfocused app has nothing to poll.

Two diagnostic traps, an hour between them:

- **`Page.bringToFront` HANGS over CDP against this nested Chrome**, and a
  `Runtime.evaluate` queued behind it looks exactly like a broken clipboard
  API. Drop it and evaluate directly. ⚠ Observed AFTER the frame-callback
  watchdog landed, so it is NOT the
  [frame starvation](./nested_compositor_frame_starvation.md) stall whose
  signature is also a render-dependent CDP call never returning — the two
  look identical and only the ordering separates them.
- When probing this Chrome from an **ad-hoc Python script** (not
  `cdp_client.py`, which uses QtWebSockets and is unaffected), pass
  `suppress_origin=True` to `websocket-client` or Chrome rejects the CDP
  upgrade with a 403.

Related: [fork-changes-need-makepkg](./fork_changes_need_makepkg.md) — any such
plugin has to be packaged before launcher-launched IDEs can load it;
[nested compositor frame starvation](./nested_compositor_frame_starvation.md);
[nested compositor pointer input](./nested_compositor_pointer_input.md).
