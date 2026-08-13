pragma ComponentBehavior: Bound

// Rounds a rectangle's corners by painting OVER them, not by clipping it.
//
// Anchor-fill this on top of a surface (highest z among its siblings) and the
// surface reads as having `cornerRadius` corners. What actually happens is that
// four small wedges — the region inside the corner square but outside the
// quarter-disc — get painted `cornerColor`, which is the colour of whatever
// surrounds the surface. The surface itself is untouched.
//
// WHY NOT THE OBVIOUS ROUTES. The central surface is a stack of opaque panes
// that each paint their own square rectangle: QMLTermWidget (Konsole's VT
// engine, editor + shell + agents), the nested Wayland compositor hosting real
// Chrome, and the FM's QML module. None of them can be persuaded to draw a
// rounded outline.
//   • `clip: true` is a RECTANGULAR scissor in Qt Quick. It cannot follow a
//     radius, so it does nothing here.
//   • `layer.enabled` + an OpacityMask would work, but it routes every frame of
//     the terminal AND of the nested compositor's ShellSurfaceItem through an
//     offscreen FBO. That is the one part of this app where frame delivery is
//     already load-bearing and fragile (see the frame-starvation watchdog in
//     native/symmetria-compositor/symmetriaoutput.h). Not worth a corner.
//   • A `Rectangle` with `radius` and a thick `border` paints the INSIDE of the
//     arc, i.e. exactly the half we must not cover. The wedge is the complement
//     of a rounded rect and no Rectangle can express it.
// So: a path. Four of them, static, tiny.
//
// ONE SHAPE, ROTATED FOUR TIMES. The wedge is drawn once in the top-left of a
// square box; `rotation: index * 90` about the box centre maps it onto each of
// the other three corners, and x/y place the box. Writing four hand-mirrored
// paths would be four chances to get a control point backwards.
//
// The arc is a cubic Bézier, not `PathArc`, on purpose. Two endpoints one
// radius apart admit two candidate centres and two sweep directions, so a
// `PathArc` here is a direction flag you can only verify by looking at it. The
// cubic states the geometry outright: leave (r,0) heading -x, arrive at (0,r)
// heading +y, with the standard quarter-circle handle length. Wrong handles are
// visible as a wrong CURVE, never as a wedge on the wrong side.

import QtQuick
import QtQuick.Shapes

Item {
    id: root

    // 0 disables the effect entirely (the Repeater still builds four
    // zero-sized Shapes, so `visible` short-circuits the paint).
    property int cornerRadius: 0

    // The colour AROUND the rounded surface, not the surface's own. The wedge
    // has to continue whatever the surface is inset from.
    property color cornerColor: "transparent"

    visible: root.cornerRadius > 0

    // This sits on top of every central surface, so it must be inert: not just
    // "has no MouseArea" but unable to acquire focus or swallow a hover as the
    // panes below change.
    enabled: false

    Repeater {
        model: 4

        delegate: Shape {
            id: wedge

            required property int index

            // Handle length for a quarter-circle cubic. The constant is
            // 4/3 * (sqrt(2) - 1) — the same approximation Qt's own rounded
            // rectangles use, so this corner matches a `Rectangle { radius }`
            // of equal size placed beside it.
            readonly property real handle: 0.5522847498307933

            width: root.cornerRadius
            height: root.cornerRadius
            // index 0 top-left · 1 top-right · 2 bottom-right · 3 bottom-left,
            // which is the order `rotation: index * 90` walks (Qt rotates
            // clockwise, about Item.Center by default — square box, so the
            // wedge lands exactly on the next corner).
            x: (wedge.index === 1 || wedge.index === 2) ? root.width - wedge.width : 0
            y: (wedge.index === 2 || wedge.index === 3) ? root.height - wedge.height : 0
            rotation: wedge.index * 90

            // Analytic antialiasing. The default renderer would need MSAA on
            // the window to keep a 24px arc from stair-stepping, and the arc
            // sits against a ~4-unit lightness step where stair-steps read as
            // dirt rather than as aliasing.
            preferredRendererType: Shape.CurveRenderer

            ShapePath {
                fillColor: root.cornerColor
                // Negative width disables stroking. A stroke would straddle
                // the arc and bleed `cornerColor` a half-pixel INTO the
                // surface all along the curve.
                strokeWidth: -1

                startX: 0
                startY: 0

                PathLine { x: wedge.width; y: 0 }

                PathCubic {
                    control1X: wedge.width * (1 - wedge.handle)
                    control1Y: 0
                    control2X: 0
                    control2Y: wedge.height * (1 - wedge.handle)
                    x: 0
                    y: wedge.height
                }

                PathLine { x: 0; y: 0 }
            }
        }
    }
}
