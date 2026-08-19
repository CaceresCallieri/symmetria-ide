---
name: append-only-pane-registry
description: "A Repeater over a plain integer destroys every delegate when the integer moves; an append-only list model does not — measured 2026-08-15, and it is why the agent pool has no cap."
metadata: 
  node_type: memory
  type: reference
  originSessionId: d98045ba-5a01-43c8-9538-cda6bcd90bfd
  modified: 2026-08-15T05:05:23.187Z
---

# A Repeater's model type decides whether growing the pool kills live agents

**Date:** 2026-08-15.
**Configuration:** this repo at the Phase-1 commit of the thread-rail work;
Qt 6.11 / PySide6 on `QT_QPA_PLATFORM=offscreen`; a real `AppController` with a
no-I/O bridge double and `shutil.which` stubbed; nine `spawn_agent("fresh", True,
"claude")` calls; delegates counting their own `Component.onDestruction`.
**Method:** `tests/qml_harness/pane_growth_probe.py`, driven by
`tests/integration/test_agent_pane_growth_qml.py`. Run it directly with
`QT_QPA_PLATFORM=offscreen PYTHONPATH=src python tests/qml_harness/pane_growth_probe.py`
— it prints `{"before": 3, "after": 9, "destroyed_initial": 0}` on the current
model and nothing else.

## What was measured

| Repeater model | Growth | Delegates destroyed | Live panes after |
|---|---|---|---|
| plain integer (`model: controller.maxAgentSlots`) | 3 → 8 | all 3 initial | 0 |
| append-only `QAbstractListModel` (`AgentPaneSlotModel`) | 3 → 9 | 0 | 9 |

Each delegate owns a `QMLTermSession`; its destructor hangs up the Pty and reaps
the agent CLI. So "delegate destroyed" means "the user's running agent died".

**Consequence:** the five-slot cap was never about memory or ergonomics. It
existed because the integer model could not grow without reaping. Removing the
cap therefore meant changing the model, not raising a constant.

## The discipline that makes a list model safe here

CLAUDE.md's older rule — "do NOT convert to a list-model Repeater" — was written
against a model of ACTIVE agents, and stays correct for that: such a model
reorders on focus and deletes on close, and either operation churns delegates.
`AgentPaneSlotModel` has neither:

- rows are only APPENDED; never removed, never moved, never reset while running;
- closing an agent FREES its slot — the row stays and its `active` role flips
  false, deactivating exactly that one Loader;
- so row `i` always describes slot `i + 1`, contiguously, which is the only
  reason `agentSlotRepeater.itemAt(slot - 1)` is valid at three call sites.

Break any of the three and the measurement above stops applying.

## A trap next to it, not part of the measurement

`QQmlComponent.setData(data, QUrl("inmemory:/X.qml"))` never leaves
`Status.Loading`: the engine treats the unknown scheme as a network URL and
queues a fetch that never resolves, so `create()` returns `None` with an EMPTY
`errors()` list — a failure with no message. `QUrl.fromLocalFile(...)` compiles
synchronously, and that is what both `tests/qml_harness/spawn_menu_probe.py` and
`pane_growth_probe.py` pass as the base URL. It cost a round trip on the first
cut of the growth probe, which used the `inmemory:` form.

**Related:** [Why the suite dies "intermittently"](./processevents_shared_app_segv.md)
on why these probes run out-of-process at all.
