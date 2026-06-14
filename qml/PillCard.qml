// CLAYMORPHISM card — the content-framing variant of PillSurface (IDE port of
// the Shell / FM PillCard). Same recipe (matte fill + hairline border + convex
// two-shadow depth + rim highlight), but tuned for FRAMING content rather than
// being a chip:
//   - rounded `lg` corners instead of a tight chip radius
//   - softer, WIDER shadows (more "embedded panel" than "raised chip")
//   - a faint bottom inner-shadow (innerShadowAlpha) for the recessed feel
//
// Implemented as a thin PillSurface with the `Theme.depth.card` preset, so the
// depth recipe lives in exactly ONE place (PillSurface). It inherits
// PillSurface's `default property alias content`, so `PillCard { SomeChild {} }`
// reparents children into the body — which is how ModalOverlay drops its content
// Column straight into the card.
//
// Use it for popups / dialogs / any framed surface. For capsule chips (the
// surface switcher, agent bubbles, dialog buttons) use PillSurface directly.

import "design"

PillSurface {
    id: root

    radius: Theme.radius.lg

    darkShadowOffsetX: Theme.depth.card.darkOffsetX
    darkShadowOffsetY: Theme.depth.card.darkOffsetY
    darkShadowBlur: Theme.depth.card.darkBlur
    darkShadowAlpha: Theme.depth.card.darkAlpha

    lightShadowOffsetX: Theme.depth.card.lightOffsetX
    lightShadowOffsetY: Theme.depth.card.lightOffsetY
    lightShadowBlur: Theme.depth.card.lightBlur
    lightShadowAlpha: Theme.depth.card.lightAlpha

    // Bottom inner-shadow visible on cards (off on chips) → embedded-panel feel.
    innerShadowAlpha: Theme.depth.card.innerShadowAlpha
}
