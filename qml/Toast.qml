// Non-modal, auto-dismissing transient banner ("toast"). Unlike ModalOverlay
// (scrim + keyboard-modal focus steal + Esc-to-dismiss), a Toast NEVER grabs
// focus or blocks the surface beneath — no MouseArea, no key handling, so
// clicks fall through and the user keeps working. It just pops in, lingers, and
// fades out on a timer.
//
// First consumer: the agent spawn-failure alert (Main.qml,
// controller.onAgentSpawnFailed) — it turns the otherwise silent chip flap of
// an agent that died at startup (appear -> vanish in ~250ms, usually an OOM
// kill) into a readable message. Kept generic (title + detail + show()) so
// later alerts can reuse it.
//
// Positioned by the host (anchors set where it's instantiated). Drive it with
// show(title, detail); it auto-hides after `autoHideMs`. Entrance + exit use the
// toolkit's scale-pop + fade (Theme.anim.*) — the same motion language as the
// modals (see feedback/popup-animation). Claymorphism frame via PillCard; all
// colour / typography / motion bind against `Theme` — no local literals.

import QtQuick

import "design"

Item {
    id: root

    // Sized to the panel so the host anchors the whole toast as one box.
    implicitWidth: panel.width
    implicitHeight: panel.height
    width: implicitWidth
    height: implicitHeight

    property string title: ""
    property string detail: ""
    property int autoHideMs: 9000

    // Animation/visibility driver. show() raises it; the timer (or a fresh
    // show()) lowers it. `visible` rides the panel's fade so it keeps painting
    // through the exit and drops out once fully transparent. Driven off this
    // plain bool — NOT off `visible` — to avoid the enabled-binding race the
    // ModalOverlay header documents.
    property bool shown: false
    visible: panel.opacity > 0

    function show(toastTitle, toastDetail) {
        root.title = toastTitle;
        root.detail = toastDetail;
        root.shown = true;
        hideTimer.restart();
    }

    function hide() {
        root.shown = false;
    }

    Timer {
        id: hideTimer
        interval: root.autoHideMs
        onTriggered: root.shown = false
    }

    PillCard {
        id: panel

        // Fixed width keeps the detail line at a readable measure; height flows
        // up from the content (anchors-top body -> implicitHeight), the same
        // height-flows-up / width-flows-down idiom ModalOverlay's panel uses.
        width: 440
        implicitHeight: body.implicitHeight + Theme.spacing.md * 2
        height: implicitHeight

        // Scale-pop + fade. transformOrigin Center so it grows from the middle.
        transformOrigin: Item.Center
        scale: root.shown ? 1 : Theme.anim.popFromScale
        opacity: root.shown ? 1 : 0
        Behavior on scale {
            NumberAnimation {
                duration: Theme.anim.duration
                easing.type: Easing.OutBack
                easing.overshoot: Theme.anim.popOvershoot
            }
        }
        Behavior on opacity {
            NumberAnimation {
                duration: Theme.anim.duration
                easing.type: Easing.BezierSpline
                easing.bezierCurve: Theme.anim.standardCurve
            }
        }

        Row {
            id: body
            x: Theme.spacing.md
            y: Theme.spacing.md
            width: panel.width - Theme.spacing.md * 2
            spacing: Theme.spacing.sm

            // Alert glyph (nf-fa-exclamation_triangle) in the EDITOR font —
            // editorFontFamily carries the nerd glyphs; the chrome font may
            // not. mode.replace red is the toolkit's "stop / something's wrong"
            // cue, shared with the editor + minimap diagnostic palette.
            Text {
                id: alertGlyph
                text: ""
                color: Theme.color.mode.replace
                font.family: editorFontFamily
                font.pixelSize: Theme.font.size.md
                renderType: Text.NativeRendering
            }

            Column {
                // Remaining width after the glyph + Row spacing → both text
                // lines then wrap within the fixed-width card.
                width: body.width - alertGlyph.width - body.spacing
                spacing: Theme.spacing.xs

                Text {
                    width: parent.width
                    text: root.title
                    color: Theme.color.text.strong
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    font.weight: Theme.font.weight.bold
                    wrapMode: Text.WordWrap
                    renderType: Text.NativeRendering
                }

                Text {
                    width: parent.width
                    text: root.detail
                    color: Theme.color.text.normal
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    wrapMode: Text.WordWrap
                    renderType: Text.NativeRendering
                }
            }
        }
    }
}
