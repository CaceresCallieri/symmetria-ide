// CLAYMORPHISM pill primitive — the IDE's port of the File Manager /
// Symmetria Shell `PillSurface`, so the IDE speaks the same toolkit-wide
// "clay" visual language (clay pills are everywhere in the Shell's bars and
// the FM's active tab / dialogs). Use it wherever a matte capsule surface is
// wanted: the surface switcher, the agent bubbles, dialog buttons, the modal
// panels (via PillCard). This is the SINGLE home of the depth recipe in the
// IDE — surfaces reach for this component, never re-derive shadow constants.
//
// The recipe (what makes the "claymorphism" look):
//   - Two opposing OUTER shadows — dark SE + light NW — produce a convex,
//     extruded "raised chip" feel. Slightly asymmetric offsets (y > x on the
//     dark shadow) read as an overhead light source.
//   - A hairline 1px border — crisp edge definition that survives the busy /
//     transparent terminal backdrop where the shadows alone would wash out.
//   - A top rim highlight (inner gradient) — the "polished, lit-from-above"
//     warmth that distinguishes clay from a flat matte rect.
//
// All depth constants come from `Theme.depth.chip` (+ `Theme.depth.highlightAlpha`);
// PillCard overrides them with `Theme.depth.card` for the softer framed look.
// Retune the aesthetic for ALL clay surfaces at once by editing those tokens.
//
// `elevated` gates the DEPTH only (both shadows + the rim highlight); the
// matte fill + border always paint. This lets a consumer present a flat matte
// pill (an inactive switcher segment, an unfocused bubble) and raise it into
// full claymorphism on a state change — the one boolean that expresses
// "especially the active one" uniformly across every clay surface.
//
// Shadow infra is Qt6-native `RectangularShadow` (QtQuick.Effects) — no
// Qt5Compat GraphicalEffects, no custom shaders. Available since Qt 6.9; the
// IDE runs Qt 6.11.
//
// GOTCHA — shadows render OUTSIDE the pill bounds. The two RectangularShadows
// are offset + blurred, so they paint into the area AROUND pillBody, beyond
// this component's own rect. Two consequences:
//   1. Any ANCESTOR with `clip: true` will slice the soft shadow edges off,
//      leaving a flat, chopped-looking pill. If a shadow looks sliced, search
//      up the parent chain for a clip — do NOT "fix" it by shrinking the blur.
//   2. The host must leave margin around the pill for the shadow to occupy.
//      On the 24px chrome bars this is why the chip preset's blur is kept
//      tight (Theme.depth.chip) — a wider blur would bleed onto the content
//      below the bar. This is inherent to drop-shadow depth, not a bug.
//
// Two usage patterns (same as the Shell / FM):
//   1. Content-inside — children declared inside PillSurface { ... } reparent
//      into the body and paint above the rim highlight (PillCard uses this for
//      the modal content Column).
//   2. Background-only — give it no children, anchors.fill a host Item, and
//      let siblings paint on top (what the switcher segments / bubbles do, so
//      the existing Row + MouseArea stay untouched). Declared FIRST so it
//      renders behind the siblings.

import QtQuick
import QtQuick.Effects

import "design"

Item {
    id: root

    // --- Fill / border (default to the IDE's chrome tokens) ----------------
    // Flat props (not a `border` group) so consumers can bind/animate them
    // directly, matching the Shell's PillSurface API.
    property color color: Theme.color.bg.chrome
    property color borderColor: Theme.color.border.hairline
    property real borderWidth: 1
    property real radius: Theme.radius.md // neutral fallback — every consumer sets radius explicitly (chips: height/2, cards: radius.lg)

    // Master on/off for the clay DEPTH (both shadows + the rim highlight).
    // The matte fill + border stay regardless. See the header note.
    property bool elevated: true

    // --- Two-shadow convex depth (defaults: Theme.depth.chip) --------------
    property real darkShadowOffsetX: Theme.depth.chip.darkOffsetX
    property real darkShadowOffsetY: Theme.depth.chip.darkOffsetY
    property real darkShadowBlur: Theme.depth.chip.darkBlur
    property real darkShadowAlpha: Theme.depth.chip.darkAlpha

    property real lightShadowOffsetX: Theme.depth.chip.lightOffsetX
    property real lightShadowOffsetY: Theme.depth.chip.lightOffsetY
    property real lightShadowBlur: Theme.depth.chip.lightBlur
    property real lightShadowAlpha: Theme.depth.chip.lightAlpha

    // --- Inner rim highlight + (optional) bottom inner-shadow --------------
    property real highlightAlpha: Theme.depth.highlightAlpha
    property real innerShadowAlpha: Theme.depth.chip.innerShadowAlpha

    // Default slot: children reparent into the body and overlay the rim
    // highlight (declared after it, so they paint on top).
    default property alias content: contentHolder.data

    // Sanctioned literal-colour exemption: the shadow black and the rim
    // highlight white below are intrinsic to the clay recipe (an overhead
    // light is white, a cast shadow is black) — physical constants, NOT
    // theme colours, so they stay `Qt.rgba` literals here. Only their ALPHAS
    // are Theme-tokened (Theme.depth.*). Don't "fix" these into Theme colour
    // tokens — they would never vary with the palette.

    // Dark shadow (bottom-right) — declared FIRST so it renders furthest back
    // (paint order is declaration order for items without explicit z).
    RectangularShadow {
        anchors.fill: pillBody
        radius: pillBody.radius
        blur: root.darkShadowBlur
        spread: 0
        offset.x: root.darkShadowOffsetX
        offset.y: root.darkShadowOffsetY
        color: Qt.rgba(0, 0, 0, root.elevated ? root.darkShadowAlpha : 0)

        // Ease the raise/settle as `elevated` toggles (state-transition
        // speed, not the slow entrance pop).
        Behavior on color {
            ColorAnimation { duration: Theme.anim.quick }
        }
    }

    // Light shadow (top-left).
    RectangularShadow {
        anchors.fill: pillBody
        radius: pillBody.radius
        blur: root.lightShadowBlur
        spread: 0
        offset.x: root.lightShadowOffsetX
        offset.y: root.lightShadowOffsetY
        color: Qt.rgba(1, 1, 1, root.elevated ? root.lightShadowAlpha : 0)

        Behavior on color {
            ColorAnimation { duration: Theme.anim.quick }
        }
    }

    Rectangle {
        id: pillBody

        anchors.fill: parent
        color: root.color
        radius: root.radius
        border.width: root.borderWidth
        border.color: root.borderColor

        // Ease active-state fill/border transitions so a chip raising into
        // clay doesn't snap.
        Behavior on color {
            ColorAnimation { duration: Theme.anim.quick }
        }
        Behavior on border.color {
            ColorAnimation { duration: Theme.anim.quick }
        }

        // Inner rim/contact gradient — top rim highlight by default; bottom
        // band off (innerShadowAlpha 0 on the chip preset, faint on cards).
        // Fades with the two shadows on the `elevated` toggle (opacity eased
        // over Theme.anim.quick) instead of snapping via `visible`, so the
        // whole clay raise eases as one. `visible` stays STATIC — it only
        // gates out presets with no rim AND no inner-shadow, so a flat-only
        // pill never instantiates the layer.
        Rectangle {
            anchors.fill: parent
            radius: pillBody.radius
            color: "transparent"
            visible: root.highlightAlpha > 0 || root.innerShadowAlpha > 0
            opacity: root.elevated ? 1 : 0

            Behavior on opacity {
                NumberAnimation { duration: Theme.anim.quick }
            }

            gradient: Gradient {
                GradientStop { position: 0.00; color: Qt.rgba(1, 1, 1, root.highlightAlpha) }
                GradientStop { position: 0.45; color: Qt.rgba(1, 1, 1, 0.00) }
                GradientStop { position: 0.55; color: Qt.rgba(0, 0, 0, 0.00) }
                GradientStop { position: 1.00; color: Qt.rgba(0, 0, 0, root.innerShadowAlpha) }
            }
        }

        // Consumer content slot. anchors.fill so children naturally use
        // anchors.fill / anchors.centerIn against the pill bounds.
        Item {
            id: contentHolder
            anchors.fill: parent
        }
    }
}
