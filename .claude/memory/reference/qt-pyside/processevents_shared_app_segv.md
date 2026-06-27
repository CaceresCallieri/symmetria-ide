---
name: processevents-shared-app-segv
description: "In tests, QCoreApplication.processEvents() on the shared session app runs prior tests' deleteLater → gotcha"
metadata: 
  node_type: memory
  type: reference
  originSessionId: 6e18d2b3-fa99-4df3-ae3c-7e2a050321cf
---

**Never call `QCoreApplication.processEvents()` (or otherwise pump the event
loop) in a unit test that runs against the shared session-scoped
`QCoreApplication`** (the `qt_app` autouse fixture in `tests/conftest.py`).

`processEvents()` drains the GLOBAL Qt event queue, which includes deferred
`deleteLater` deletions queued by EARLIER QML-heavy test modules
(`test_browser_surface_qml`, `test_bootstrap`, etc.). Running those deletions
mid-suite trips the Python-3.14 cyclic-GC-vs-Qt teardown SEGV (CLAUDE.md
gotcha #10). Symptom: the test file passes in ISOLATION but the FULL suite
exits 139 with a faulthandler dump whose Current-thread C stack sits in
`QEventDispatcherGlib::processEvents` → `g_main_context_iteration`. Burned
2026-06-26 adding GitController's cold-start recovery test (see
[GitController cold-start recovery](../../project/shipped/gitcontroller_cold_start_recovery.md)).

**How to test a `QueuedConnection` deterministically instead:**
- Spy on the emit with a plain `signal.connect(list.append)` — a same-thread
  DIRECT connection captures the payload synchronously, no loop needed.
- Deliver the slot BY HAND: `controller._refresh_watcher_for_root(payload)`.
  This exercises the exact payload the real queued connection carries, is
  order-independent, and faster.

The production code's queued connections are correct and necessary
(worker→GUI marshaling per project-standards §4 P2) — this rule is about
TEST harnesses only, not the runtime wiring.
