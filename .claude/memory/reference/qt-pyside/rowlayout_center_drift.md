---
name: RowLayout center-drift on partially-hidden rows
description: In QtQuick.Layouts.RowLayout, hiding the only fillWidth item without explicit Qt.AlignLeft drags visible items toward the row's center; fix is two reinforcing defenses.
type: reference
---

In `QtQuick.Layouts.RowLayout`, two layout behaviors compound to produce a confusing
"visible items drift toward the row center" symptom when items hide and show across
modes:

1. **No fillWidth claimant ⇒ trailing cell grows.** When the only `Layout.fillWidth: true`
   item is hidden (`visible: false`) and no other item claims fillWidth, the layout
   doesn't pack items at the leading edge with leftover space on the trailing edge —
   it grows the LAST visible item's cell to absorb the leftover space.
2. **Bare `Qt.AlignVCenter` is horizontally unset.** `Qt.AlignVCenter == 0x80` — only
   the vertical-center bit. With no horizontal bit set, items in their (now-grown)
   cell render horizontally centered, not flush-left.

Net effect: in our `StatusBar.qml`, when the file Text (the only fillWidth item) hid
in terminal mode, the branch's cell grew to fill the whole row's leftover space and
the branch text centered within that cell — appearing to "drift" to the row's middle
even though branch + project should logically pack flush against the leading edge.

**Why:** `Layout.fillWidth: visible` alone doesn't fix this — collapsing the hidden
item's slack claim removes the immediate cause (no item competing for fill), but
also removes the LAST fillWidth item entirely, which triggers the cell-grow fallback
on whatever item happens to be last in visible-order. Both behaviors stack; both
need to be defused.

**How to apply (two reinforcing layers):**

1. **Always set both alignment bits explicitly:** `Layout.alignment: Qt.AlignLeft | Qt.AlignVCenter`
   on every item that's not deliberately right-aligned. Bare `Qt.AlignVCenter` is a
   silent footgun — it looks complete because vertical is the only axis you remembered
   to think about. Make horizontal explicit so cell-grown fallbacks render at the leading
   edge.
2. **Keep at least one fillWidth claimant alive in every mode.** If your "main"
   fillWidth item gates on a condition (visible/active/etc.), add a sibling spacer
   `Item { Layout.fillWidth: !mainItem.visible }` (or equivalent) so the row always
   has exactly one fillWidth claimant. The spacer absorbs leftover space and keeps
   visible items at their cell sizes, preventing the cell-grow path entirely.

Either defense alone is fragile (the spacer reintroduces "phantom fillWidth on hidden
items" if you forget to gate it; explicit AlignLeft only helps when the cell actually
grows). Both together survive every order-of-hiding case I tested in StatusBar.qml.

**Canonical reference:** `qml/StatusBar.qml` — the comment block above
`Rectangle { id: modeBadge ... }` and around `Item { Layout.fillWidth: !root.editorActive }`
are the durable in-code reminders. Both blocks call out that reverting them returns
the centering bug.

**Symptom signature for future detection.** If a row of status-bar-like items drifts
toward the middle when one of them hides, your first suspect is "I lost my fillWidth
claimant AND my items have bare `Qt.AlignVCenter`." Apply both fixes; don't try to
diagnose which one is sufficient in your specific case — both are cheap, and the
mode-coverage of a real status bar makes empirical bisection costly.
