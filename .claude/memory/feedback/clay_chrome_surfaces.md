---
name: clay-chrome-surfaces
description: New IDE chrome surfaces use the shared clay primitives (PillSurface/PillCard + Theme.depth), gating depth on active-state via `elevated`; don't hand-roll flat Rectangles or per-component shadows
metadata:
  node_type: memory
  type: feedback
  originSessionId: 340b48c0-97e9-48c4-a8b6-9f87c80785b8
---

The IDE's chrome uses **claymorphism** — the File Manager / Symmetria Shell
"clay" depth recipe (matte fill + hairline border raised by two opposing
outer shadows + a top rim highlight). The user asked (2026-06-14, while
polishing before stable promotion) to **establish this forward**: every
surface built from here on should take the clay look into account, not just
the ones first restyled.

The recipe lives in ONE place — reach for it, never re-derive shadow
constants:

- `qml/PillSurface.qml` — the clay capsule primitive (chips/buttons/bars).
  Background-only usage: declare it `anchors.fill: parent` behind the
  existing content Row + MouseArea (what the surface switcher / agent
  bubbles / dialog buttons do).
- `qml/PillCard.qml` — the framed-panel variant (softer/wider shadows +
  faint bottom inner-shadow); used as the `ModalOverlay` panel, so every
  modal (spawn menu, session picker, close-confirm) inherits clay.
- `Theme.depth` tokens in `qml/design/Theme.qml` — `chip` preset (compact
  chrome) + `card` preset (modals) + `highlightAlpha`. `Theme.anim.quick`
  (150ms) eases the state transitions.

**The `elevated` boolean is the key idiom**: a flat matte pill and a fully
raised clay chip are the SAME component in two states. Bind `elevated` to
the surface's active/focused/highlighted state — that's how "especially the
active one should have the look" is expressed uniformly (the FM active-tab
pattern). Inactive = flat (often transparent fill + border); active = raised.

Depth uses Qt6-native `RectangularShadow` (`import QtQuick.Effects`, Qt 6.9+;
IDE runs 6.11). Shadows render OUTSIDE the rect — an ancestor `clip: true`
slices them; the host must leave margin (why the chip preset's blur is tight
for 24px bars).

**Why:** one shared toolkit recipe keeps the Symmetria IDE/FM/Shell visually
coherent and makes a retune a single-token edit (DRY — the depth is the same
logic everywhere). The user's explicit forward-looking directive means new
chrome that ships as a flat Rectangle is a regression against the
established language.

**How to apply:** for a new chrome surface, wrap/back it with `PillSurface`
(or `PillCard` for a framed panel), set `radius`/`color`/`borderColor` from
Theme, and bind `elevated` to its active state. Don't add a bare
`Rectangle { radius; color }` chip or a per-component shadow. Tune intensity
via `Theme.depth.*`, not inline literals. See also
[popup entrance motion](./popup_animation.md) (clay is the *surface*; scale-pop
is the *entrance* — both apply to a new popup) and
[UI surface discipline](./ui_surface_discipline.md).
