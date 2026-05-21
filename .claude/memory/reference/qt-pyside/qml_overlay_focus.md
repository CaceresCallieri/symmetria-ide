---
name: QML overlay focus discipline
description: When opening an overlay above a FocusScope (NvimView/TerminalView), don't blanket forceActiveFocus on wrapper Items — Component.onCompleted ordering causes the wrapper to steal focus FROM the inner widget that should hold it.
type: reference
---

# QML overlay focus discipline

When opening a QML overlay above a Window that already contains `FocusScope` Items (NvimView and TerminalView both set `ItemIsFocusScope` in their `QQuickPaintedItem` subclasses), do NOT add a blanket `Component.onCompleted: forceActiveFocus()` on the overlay's outer wrapper Item.

## The trap

QML fires `Component.onCompleted` **children-first, then parents**. So if a deep child (e.g. a `ListView` inside the loaded subtree) imperatively claims focus via `view.forceActiveFocus()` in ITS `onCompleted`, that runs FIRST. Then the wrapper Item's `onCompleted: forceActiveFocus()` runs LATER and steals focus back to the wrapper — which is a plain `Item` (not a `FocusScope`), so focus stops there and never propagates back down. Result: the inner widget that had carefully claimed focus to wire up its key handlers is now silently dead, and keys land on the wrapper which has only partial Keys handlers (typically Esc + a literal close key).

## How it presents in practice

The classic symptom triad:
1. Esc dismisses the overlay correctly (handled at the wrapper level OR via a passthrough panel hook).
2. A single literal key (e.g. `q`) dismisses correctly (bubbles up from the panel to the wrapper because the panel doesn't accept it).
3. Navigation keys (arrows, j/k/h/l) do nothing visible — they go to the wrapper which has no handler for them.

If you also see a `terminal_view` / `nvim_view` key log STOP appearing after the overlay opens (no events being logged for ignored keys), it confirms focus has moved INTO the overlay subtree — it's just landed on the wrong element within it.

## How to diagnose

1. Enable `SYMMETRIA_IDE_KEY_TRACE=1` and confirm whether key events still reach NvimView/TerminalView after the overlay opens. If they don't, focus IS inside the overlay — the question is just which element.
2. Inspect the overlay's wrapper Item for `Component.onCompleted: forceActiveFocus()` and ask whether any descendant ALSO calls `forceActiveFocus()` in its own onCompleted (look in the installed module's source, not just our QML).
3. The parent-level forceActiveFocus is almost always wrong if a child already self-focuses.

## The fix

Remove the wrapper's `forceActiveFocus()`. Trust the inner widget's claim. Verify the bubble-up still works for the close keys (Esc / literal q):
- Esc usually has its own handler inside the panel (e.g. `NormalModeHandler.js:22` for the FM in this codebase).
- A literal close key like `q` bubbles up because the panel's NormalModeHandler doesn't accept it (it only handles `Ctrl+Q`), so the unaccepted KeyEvent propagates UP the focus chain to the wrapper's `Keys.onPressed`.

If — and only if — the inner widget does NOT self-focus on construction, you need to push focus from outside. In that case do NOT use a blanket `forceActiveFocus()` on a plain `Item`; either:
- Use `FocusScope` as the wrapper and put `focus: true` on a child that's itself a `FocusScope`, OR
- Push focus directly on the deepest reachable `FocusScope` descendant (via `id` reference).

A `Loader` is NOT a `FocusScope` by default — `Loader.focus = true` + `Loader.forceActiveFocus()` will still land on the Loader itself, not propagate down. Don't rely on that pattern.

## Why this is a recurring class of bug

The same lesson surfaces in two directions in this codebase:

1. **Focus return on DISMISS** (already encoded in `qml/Main.qml`'s `onFmVisibleChanged` handler, around the comment "Focus return on dismiss"): without explicit `forceActiveFocus()` back to the correct central pane, focus stays on the now-destroyed overlay subtree's parent and keystrokes go nowhere — nvim/agent appears frozen until alt-tab.

2. **Focus push on OPEN** (this file): the inverse problem. Without explicit AND CAREFULLY TARGETED focus push, focus lands on the wrong element of the overlay, breaking inner navigation while leaving the close keys functional (which makes the bug look milder than it is).

Both directions need EXPLICIT, MINIMAL focus management — every IDE-wide chord that opens a new surface should think carefully about which element should hold `activeFocus` and avoid the temptation to "just `forceActiveFocus` at the top and let it propagate."

## Reference incident

Diagnosed empirically on 2026-05-20 when the file-manager trigger was promoted out of the nvim layer (`<leader>e` / `<C-u>` Lua hijack → IDE-wide `Ctrl+E` `ApplicationShortcut`). The original `<C-u>` path worked by accident: nvim relinquished its focus claim during the keymap → rpcnotify roundtrip, leaving the way clear for FileList's own `view.forceActiveFocus()`. The new chord, firing as an `ApplicationShortcut` from outside the editor pane (typically with TerminalView focused), exposed the focus-stealing wrapper.

Fix landed by removing `Component.onCompleted: forceActiveFocus()` from the `fmOverlay` wrapper Item in `qml/Main.qml`. See the in-file comment at that site for the per-call-site rationale; this file is the cross-cutting "always think about this" pointer.
