---
name: nested-compositor-pointer-input
description: Pointer input in the nested-Chrome pane needs THREE non-default things — two fatal to scrolling, one to motion; all mislead as focus bugs
metadata: 
  node_type: memory
  type: reference
  originSessionId: 1dc16f14-6524-437a-9b81-8d0fde68876c
  modified: 2026-08-04T16:44:03.428Z
---

Pointer input into the IDE's nested-Wayland browser pane needs three things
that Qt does not do by default. **(2) and (3) are each independently fatal to
SCROLLING; (1) is fatal to pointer MOTION** — hover states, hover-opened menus,
cursor shape. The split is not a nicety: (1) was written in QML against a
property that does not exist, so it never ran for weeks while scrolling worked
perfectly, and an earlier version of this file called all three fatal to
scrolling. That claim is retracted.

What they DO share — the reason this cost several rounds — is the same
misleading symptom: clicking works and dragging a page's scrollbar works, so
every one of them reads as a focus bug. Press and move touch none of the three
paths.

Live code: `native/symmetria-compositor/symmetriashellsurfaceitem.{h,cpp}` —
all three live there and carry the full argument inline. This file is the "why
does this look wrong but is correct" record for anyone tempted to simplify one
of them. `qml/browser/BrowserPane.qml` retains only a comment recording why (1)
must NOT go back to QML.

## 1. No pointer motion at all — hover is never enabled

`QWaylandQuickItem` implements `hoverEnterEvent`/`hoverMoveEvent`, the only two
callers of `sendMouseMoveEvent` — which is what tells the client where the
pointer is — but it never sets `acceptHoverEvents`, and neither does
`QWaylandQuickShellSurfaceItem` (both verified against upstream Qt **6.8**
source). On a stock item neither handler fires, so the client learns the
pointer position only when a button goes down: no link hover states, no
hover-opened menus, no cursor shape, no row highlighting in the omnibox
dropdown.

**Scrolling survives without it**, because `mousePressEvent` calls
`sendMouseMoveEvent` itself — which is why an inert hover fix went unnoticed
for weeks, and why "hover seems to work" is an unreliable observation here
(hover states update at each click and freeze in between).

Fix: **`setAcceptHoverEvents(true)`, in C++** — `SymmetriaShellSurfaceItem`'s
constructor, walking its own children recursively for the popups Qt builds
itself (`maybeCreateAutoPopup`). It sweeps existing children as well as watching
`childrenChanged`, since a popup can parent a sub-popup during its own
construction, and the watch uses a member slot with `UniqueConnection` because
the sweep re-runs over the whole subtree and a lambda would stack connections.

⚠ **This was first written in QML as `item.hoverEnabled = true`, and it never
ran.** No Wayland item class has that property — grepping the INSTALLED **6.11**
headers, the Q_PROPERTY is declared only in `qquickmousearea_p.h` and three
QtQuickTemplates2 headers. (Two Qt versions appear in this file on purpose: the
"nothing calls `setAcceptHoverEvents`" claim was read from upstream 6.8 source,
which is what is fetchable; the property-declaration claim from the 6.11
headers actually installed here.) So
the assignment threw on the walk's FIRST line, aborting it before the recursion
or the signal connect. The entire symptom was one "Cannot assign to
non-existent property" per session, and an inert hover walk is indistinguishable
from a working one by reading it. `setAcceptHoverEvents` is a method with no QML
equivalent, which is exactly why it belongs in the subclass alongside (2) and
(3). The generalisable half is now a rule:
`.claude/rules/qml_property_must_exist_on_type.md`.

## 2. The wheel value is truncated to zero

`QWaylandPointer::sendMouseWheelEvent` ends in
`wl_fixed_from_int(-delta / 12)` — truncating integer division — so any
`angleDelta` under 12 becomes a zero-valued axis event: sent, accepted, doing
nothing. Only a classic detented wheel (120 per notch) survives it.

This is not an edge case on modern hardware. Measured on the machine where it
was found: the mouse advertises `REL_WHEEL_HI_RES` and **not** `REL_WHEEL`, so
it cannot emit a 120-unit step at all, and the touchpad reports no wheel axis
whatsoever. Both scroll entirely in fragments.

Fix: accumulate and forward whole multiples of 12, carrying the remainder.
Hand the re-quantised event to the BASE implementation rather than
reimplementing the send — Qt keeps the input-region and focus checks, and there
is one send path. Two traps in the accumulation: reset the carry on
`ScrollBegin` and on a direction reversal (a stale remainder otherwise eats up
to a full grain of the new direction), and restore it when the base REFUSES the
forwarded event (no surface / outside the input region), because in that case
no axis was sent.

## 3. Chromium never dispatches on the axis — it waits for `wl_pointer.frame`

The one with no symptom on our side at all: the axis events are sent, correct,
and traceable. `WaylandEventSource::OnPointerAxisEvent` only ACCUMULATES into
`pointer_scroll_data_`, and its single flush call site,
`ProcessPointerScrollData()`, runs inside `OnPointerFrameEvent()`. No timeout,
no other trigger. Qt's compositor never sends a frame, so every scroll piles
into a buffer nothing empties. Buttons and motion are unaffected because
Chromium dispatches those immediately unless a feature flag says otherwise.

Fix: send `wl_pointer.frame` after the axis. **This is a deliberate protocol
deviation** — `frame` is a wl_pointer v5 event and Qt hardcodes its seat global
at version 4 (`QWaylandSeat::initialize`) — and it needs PRIVATE Qt headers,
since `send_frame` has no public equivalent. It is received anyway: Chromium
registers `.frame` in its listener unconditionally, and libwayland-client
demarshals by opcode without consulting the bound version. Both read from
source; the negotiated version was read off a live `WAYLAND_DEBUG=1` trace.

What contains the risk: this compositor has exactly ONE client, the Chrome the
IDE spawns itself on a socket named after its own pid.

Advertising v5 properly is worse, not better: frames then become MANDATORY for
every pointer event group, so motion, buttons, enter and leave would all need
them too, and the version bump itself means reimplementing
`QWaylandSeat::initialize()` against private internals. Strictly more private
API for strictly more risk. Remove the deviation the day Qt's compositor
advertises 5 and sends its own frames.

## Debugging

`SYMMETRIA_COMPOSITOR_DEBUG=1` traces each wheel event on stderr (angleDelta,
pixelDelta, phase, accumulated carry, whether the item has a surface) plus both
clipboard directions. It goes to stderr rather than Qt logging because the IDE
installs a message handler that swallows `qWarning` and QML `console.log`. That
trace is what separated "the wheel never arrived" from "it arrived and was
thrown away" — two opposite causes with one symptom.

Related: [nested compositor clipboard](./nested_compositor_clipboard.md).
