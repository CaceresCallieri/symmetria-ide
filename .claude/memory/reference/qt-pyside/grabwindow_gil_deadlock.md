---
name: grabwindow-gil-deadlock
description: Synchronous QQuickWindow.grabWindow() from Python deadlocks against the GIL when any Python QQuickPaintedItem is in the scene (threaded render loop)
metadata: 
  node_type: memory
  type: reference
  originSessionId: 24d3d78f-ecfb-4678-b251-3411e2a2621a
---

# grabWindow + Python QQuickPaintedItem = ABBA deadlock

Calling `QQuickWindow.grabWindow()` (or any render-blocking sync Qt call) from
Python under the default **threaded** scene-graph render loop deadlocks when
the scene contains any Python-derived `QQuickPaintedItem` (here: `MinimapView`,
even while `visible: false` — a forced grab syncs the whole scene graph):

- Main thread: inside a Python slot → `grabWindow()` blocks on a wait
  condition for the render thread — **PySide's binding does NOT release the
  GIL while blocked**.
- Render thread: paints the scene → hits the Python painted item →
  `Sbk_GetPyOverride("paint")` → `PyGILState_Ensure` → waits for the GIL.

Intermittent (only fires when the Python item's node needs syncing — e.g. the
window's *first ever* render because it launched on a hidden workspace).
Surfaces as a Hyprland "Application Not Responding" dialog; the process can't
even handle SIGTERM (the GIL holder is stuck inside a C++ call, so Python's
signal handler never runs). Diagnose with `sudo gdb -p <pid> -batch -ex
'thread apply all bt'` (Yama `ptrace_scope=1` blocks non-sudo attach); look
for `QSGRenderThread` in `PyEval_RestoreThread`.

**Why not grabToImage:** `QQuickItem.grabToImage()` is async (no GIL hold),
but it does NOT force a render — on a hidden workspace (workspace-6 rule) the
compositor never requests frames and `ready` never fires.

**The working combination:** force `QSG_RENDER_LOOP=basic` before
`QGuiApplication` construction for any code path that needs a synchronous
grab. Single-threaded rendering makes `grabWindow()` render on the calling
thread (`PyGILState_Ensure` is re-entrant same-thread → no deadlock) AND
forces rendering regardless of expose state. Shipped in `app.py::run`
(env-gated on `SYMMETRIA_IDE_SCREENSHOT`) + `bootstrap.py::_grab_and_exit`.

**Why:** future grab/screenshot/thumbnail features will face the same triad
(sync grab / GIL / hidden window); the failure is intermittent and looks like
a random hang, so it's expensive to rediscover.

**How to apply:** never call render-blocking sync Qt APIs from Python while a
Python `QQuickPaintedItem` exists in the scene under the threaded loop. Either
force the basic loop for that process, or remove/replace the Python painted
item (C++ items don't need the GIL).
