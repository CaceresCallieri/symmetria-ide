// Framed card — the content-framing variant of PillSurface (IDE port of the
// Shell / FM PillCard), tuned for FRAMING content rather than being a chip:
//   - rounded `lg` corners instead of a tight chip radius
//   - a RAISED fill, one lightness rung above the chrome
//
// ⚠ The depth is switched off. This was a claymorphism card (wider, softer
// shadows plus a bottom inner-shadow for an "embedded panel" feel); the
// flat-aesthetic move zeroed every alpha in `Theme.depth.card`, so what used to
// say "this floats above the chrome" is now the FILL alone. That is why
// `color` is pinned to `bg.raised` here rather than inheriting PillSurface's
// `bg.chrome` default — without it a modal would be the exact colour of the bar
// behind it and would lose its edge entirely. See docs/flat-aesthetic-plan.md.
//
// Implemented as a thin PillSurface so the (now inert) depth recipe still lives
// in exactly ONE place. It inherits PillSurface's `default property alias
// content`, so `PillCard { SomeChild {} }` reparents children into the body —
// which is how ModalOverlay drops its content Column straight into the card.
//
// Use it for popups / dialogs / any framed surface. For capsule chips (the
// surface switcher, agent bubbles, dialog buttons) use PillSurface directly.

import "design"

PillSurface {
    id: root

    radius: Theme.radius.lg
    color: Theme.color.bg.raised

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
