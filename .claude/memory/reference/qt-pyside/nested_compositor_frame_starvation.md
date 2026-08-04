---
name: nested-compositor-frame-starvation
description: A nested Wayland client stalls forever when the host window stops rendering — Qt drives frame callbacks only from the render loop
metadata: 
  node_type: memory
  type: reference
  originSessionId: 1dc16f14-6524-437a-9b81-8d0fde68876c
  modified: 2026-08-04T05:44:40.940Z
---

All measurements below: **2026-07-28**, dev IDE on Hyprland workspace 6 with
workspace 10 active. rAF counted with a `requestAnimationFrame` loop driven
through CDP `Runtime.evaluate`; screenshots via `chrome-devtools-mcp`; CPU
from `/proc/<pid>/stat` deltas over a fixed window.

A nested Wayland client gets frame callbacks from ONE place: the host window's
render loop. `QWaylandQuickOutput::initialize()` wires
`QQuickWindow::beforeSynchronizing → updateStarted() → frameStarted()` and
`afterRendering → doFrameCallbacks() → sendFrameCallbacks()`. Nothing else ever
sends one.

So when the host stops rendering, the client stops **permanently**. It is a
deadlock, not slowness: the only thing that would send the next callback is the
render that is no longer happening. Live code and the full derivation:
`native/symmetria-compositor/symmetriaoutput.{h,cpp}`.

## Recognising it

The signature is a clean **zero**, not a low number, and it does not look like a
rendering problem from the client's side:

- `requestAnimationFrame` counted **0 ticks in 1007ms** while
  `document.visibilityState` still said `"visible"` — the page has no idea.
- `Page.captureScreenshot` never returns (it waits for a frame commit). Measured
  at exactly 180.0s four times, which is chrome-devtools-mcp's protocol timeout,
  not a property of the bug.

Both are indistinguishable from a compositor that "broke", which is why the
first diagnosis chased the wrong layer.

## The trap: "hidden" is not the trigger

An inactive workspace is the obvious case, but a Qt window renders only when
something in it is **dirty**. An IDE in full view showing an idle terminal
renders a handful of frames a second and starves the client just as well.
Measured at ~3.5fps in exactly that state — derived, not sampled directly:
the watchdog found no host frame on 66 of ~80 ticks at 20Hz. Any check narrowed to workspace or
window visibility will miss it.

An earlier measurement in this project concluded the opposite ("hiding the
browser surface is free; do not add machinery to keep it alive") and was written
into CLAUDE.md. It was wrong, and it cost a re-diagnosis that started by
doubting the compositor. Treat a recorded "we measured this and it is fine" as a
claim to re-run, not a fact, when the symptom says otherwise.

## What works

A watchdog, not a replacement: a GUI-thread timer that checks whether a real
scene-graph frame happened since the last tick, does nothing if one did, and
otherwise calls `frameStarted()` + `sendFrameCallbacks()` itself — the same
pair, in the same order, that the render loop would have called. rAF went 0 →
61/s and screenshots 180s → 0.1s.

Four things that are easy to get wrong:

- **`frameStarted()` and `sendFrameCallbacks()` are plain public methods, not
  slots**, so QML cannot reach them. Same class of gap as the other three
  reasons this compositor plugin exists in C++ at all.
- **Wire it from `initialize()`, not from `compositorChanged`.** With the output
  declared inside the compositor in QML, that signal never arrives, so a watch
  hung off it alone stays unarmed and the fix silently does nothing.
- **Gate it on a client actually having a surface** (`compositor()->surfaces()`).
  The pane's compositor is built at startup and never torn down, so an ungated
  timer ticks for the whole session in every IDE — and this user runs ~10.
  Connect `surfaceAboutToBeDestroyed` **queued**: it fires while the surface is
  still in `surfaces()`, so a direct re-count never reaches zero. Symptom if
  missed: the watchdog never disarms after the last surface goes — the exact
  permanent-timer cost the gate exists to prevent, visible as
  `SYMMETRIA_COMPOSITOR_DEBUG=1` traces still pumping after Chrome has exited.
- **Do not try to force the host to render instead.** The same deadlock exists
  one level up — Hyprland sends no frame callbacks to a window it is not
  displaying, so Qt's render loop is blocked too.

## Cost, measured

Frame callbacks PERMIT drawing, they do not force it — a client asks for one
only when it has something to draw. A static page is therefore free at any rate.
The pathological end is ~9% of one core: a full-screen `hue-rotate` CSS
animation injected into a static page, repainting every frame with the pane
invisible, summing the IDE and Chrome browser-process `/proc/<pid>/stat`
deltas over 4s. That asymmetry is what makes a fixed 20Hz
interval defensible instead of something adaptive.

Related: [nested compositor output mode](./nested_compositor_output_mode.md),
[nested compositor pointer input](./nested_compositor_pointer_input.md).
