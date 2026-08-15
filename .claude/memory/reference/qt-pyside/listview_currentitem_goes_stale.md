---
name: listview-currentitem-goes-stale
description: "Right after a row is removed, ListView.currentItem can still be the recycled delegate of the departed row — measured 2026-08-15; read the model by index instead."
metadata:
  node_type: memory
  type: reference
---

# `ListView.currentItem` is a stale read straight after a row removal

**Date:** 2026-08-15.
**Configuration:** this repo at the Phase-2 (thread rail) work; Qt 6.11 /
PySide6 on `QT_QPA_PLATFORM=offscreen`; the real `AgentThreadModel` and the
real `qml/AgentThreadRail.qml` inside a `Window`, its `ListView` with
`reuseItems: true`; a controller double recording every dispatch.
**Method:** load the rail with two rows (slots 1 and 2), click row 2 so
`currentIndex` is 1, then call `set_rows` with a single row — dropping the
row at index 1 and moving the survivor's `slot` role to 0 — then
`app.processEvents()` and press Enter.

**Measured:** `count` was 1 and the model's row 0 held `slot == 0`, while
`currentIndex` was still **1** and `currentItem.slot` still read **2** — the
recycled delegate of the row that had just been removed. Enter therefore
dispatched `focus_agent(2)`, focusing an agent that no longer existed. Reading
the same selection through the model instead (`agentThreads.slot_at(
threadList.currentIndex)`, which answers 0 for any out-of-range row) dispatched
nothing, which is correct.

**Why it misleads:** every intermediate state reads as healthy. The model is
right, the view's `count` is right, and the delegate is a real live object
with a plausible value on it — it is simply describing a row that is gone.
Nothing warns, and the failure only appears in the one frame between a removal
and the view's own clamp, which is exactly the frame a keypress can land in
when the removal was caused by a keypress (`x` closes an agent, the row leaves,
Enter follows).

**How to apply:**

- Resolve a selection to DATA through the model (`data(index(row, 0), role)`,
  or a small `@Slot(int, result=...)` accessor), not through
  `currentItem.<role>`. The model cannot lag its own mutation.
- Make the out-of-range answer the same as the harmless answer — `slot_at`
  returns 0, and 0 already means "no live pane" — so a clamp that has not run
  yet degrades to a no-op rather than to a wrong dispatch.
- `currentItem` is still fine where the delegate itself is the event source: a
  click handler runs ON the delegate that was clicked, so its properties are
  by construction the row the user hit.
- Suspect this whenever a list mutates from the same key handler that reads
  its selection. `reuseItems: true` widens the window (the delegate survives
  in the reuse pool instead of being destroyed), but the stale `currentIndex`
  is the root cause and is present without it.

Related: [append-only pane registry](./append_only_pane_registry.md) — the
other half of why the rail's model and the pane registry must stay separate.
