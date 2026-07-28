---
name: processevents-shared-app-segv
description: "Why the pytest suite dies non-deterministically: pumping the shared session app, and leaked AppControllers pinned by their own worker threads"
metadata: 
  node_type: memory
  type: reference
  originSessionId: 6e18d2b3-fa99-4df3-ae3c-7e2a050321cf
  modified: 2026-07-28T04:00:05.047Z
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

## The violation that proved it: `test_agent_events.py` (FIXED 2026-07-28)

`tests/test_agent_events.py` kept a `_pump_events` helper across nine call
sites and was the suite's main source of intermittent failure. Measured
2026-07-27: the full suite died inside that file in **4 of 9** runs — as a
HANG, as exit 139 (SEGV) and as exit 134 (glibc aborting on a corrupted heap),
i.e. the same race surfacing three different ways. The file passed in
ISOLATION every time, which is the signature described above and is why the
blame kept landing on whatever had been edited last.

Fixed by the prescription above: every spy now connects with an explicit
`Qt.ConnectionType.DirectConnection` and waits via `conftest.wait_until`, which
sleep-polls and never pumps. `tests/test_agent_bridge.py` got the same
treatment. **`tests/test_no_event_pumping.py` now enforces it** — an AST walk
over every test module, so the rule fails at the moment a pump is written
instead of surfacing days later as a crash somewhere else.

Still worth knowing:

- **It is load-sensitive.** The failure rate rose sharply while an unrelated
  agent's Playwright run held the machine at load 11–16. Do not read a clean
  run on an idle machine as proof of anything.
- **Narrowing the pump to `sendPostedEvents(None, QEvent.Type.MetaCall)` does
  NOT work** — tried, still 3/3 failures. `DeferredDelete` is not the whole
  story, so there is no "safer pump" to reach for.
- **A GUI-thread `QTimer` is the one thing that genuinely needs a loop.** The
  answer is to call the timer's own slot by hand and assert `isActive()`
  separately (see the title-debounce tests in `test_agent_bridge.py`), not to
  make an exception for it.

Do not attribute a failure in this area to whatever you just changed without
first re-running with your change stashed — an interleaved A/B is what proved
the browser work innocent, after two smaller samples had wrongly implicated it.

## The OTHER cause, found the same day: leaked AppControllers

Removing the pumps did not make the suite green. It hung again, and this time
the stall was diagnosable: `py-spy` put the main thread on a futex inside
`QFileSystemWatcher()` construction, with **230 live `AppController`s, 1159
threads and 224 inotify instances in one pytest process** — at only 38% through
the run, against `fs.inotify.max_user_instances = 1024` that is SHARED with the
developer's running desktop.

Seven test modules construct an `AppController` and never call `shutdown()`.
The load-bearing detail is WHY that leaks rather than merely littering: a
`threading.Thread(target=self._run_loop)` stores a **bound method**, so a
running worker holds a strong reference to its controller. An un-stopped worker
therefore pins the entire object graph behind it, `QFileSystemWatcher` and
inotify fd included. Fixed by `conftest._release_app_controller_workers`
(autouse; stops the five worker-owning sub-controllers). A/B over the same 194
tests: **779 threads alive at session end without it, 4 with it**; the full
suite went from wedging to 1691 passed in 25s.

Generalise from this rather than from the specific fixture: **"intermittent"
was the wrong word.** It was a monotonic resource ramp crossing a ceiling
shared with the rest of the machine, which is exactly why load made it worse
and why a single file in isolation always looked clean. When a suite failure
is load-sensitive and isolation-clean, count a resource (`ls /proc/<pid>/task |
wc -l`, inotify fds under `/proc/<pid>/fd`) before theorising about a race.
