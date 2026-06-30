// Non-modal, auto-dismissing transient banner ("toast"). Unlike ModalOverlay
// (scrim + keyboard-modal focus steal + Esc-to-dismiss), a Toast NEVER grabs
// focus or blocks the surface beneath, so the user keeps working — clicks fall
// through EXCEPT on a persistent error (see `severity` below), whose dismiss
// affordance is the one place a Toast accepts a click. It just pops in,
// lingers, and fades out on a timer (unless persistent).
//
// First consumer: the agent spawn-failure alert (Main.qml,
// controller.onAgentSpawnFailed) — it turns the otherwise silent chip flap of
// an agent that died at startup (appear -> vanish in ~250ms, usually an OOM
// kill) into a readable message. Second consumer: the git pull/push status
// toast (gitOpsToast) — the running/success/error feedback for the history
// view's p / P actions. Kept generic (title + detail + severity + show()) so
// later alerts can reuse it.
//
// SEVERITY drives both the glyph/accent AND the auto-hide policy:
//   - "alert"   (default) — red triangle; auto-hides. The spawn-failure look.
//   - "running" — calm-blue spinner; PERSISTS (no timer) until replaced by the
//                 finished state. "we're doing something".
//   - "success" — green check; auto-hides. "it worked".
//   - "error"   — red times-circle; PERSISTS until the user dismisses it
//                 (click / Esc / a fresh op) — never auto-vanishes, so a git
//                 error message can't fade before it's read (the explicit ask).
//
// Positioned by the host (anchors set where it's instantiated). Drive it with
// show(title, detail, severity); severity defaults to "alert" so the existing
// 2-arg callers (spawn failure) are unchanged. Entrance + exit use the
// toolkit's scale-pop + fade (Theme.anim.*) — the same motion language as the
// modals (see feedback/popup-animation). Claymorphism frame via PillCard; all
// colour / typography / motion bind against `Theme` — no local literals.
//
// LIMITATION (v1): single-instance, latest-wins. A fresh show() before the
// previous toast hides overwrites its title/detail and restarts the timer, so
// several near-simultaneous failures (e.g. a batch of agents OOM-killed at
// once) collapse to one message. Acceptable today because the spawn-failure
// detail is near-identical across a memory-pressure cascade (the user still
// learns "out of memory"), and git ops are single-flight (one pull/push at a
// time); revisit with a count or a queue if a consumer needs every message.

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

    // One of "alert" | "running" | "success" | "error" (see header). Drives the
    // glyph, the accent colour, and whether the toast auto-hides.
    property string severity: "alert"

    // Persistent severities hold until replaced/dismissed — they get NO
    // auto-hide timer. "running" is replaced by the finished state; "error"
    // waits for the user (the explicit "don't auto-disappear on error" ask).
    readonly property bool _isPersistent: root.severity === "running"
                                          || root.severity === "error"

    // Accent (glyph colour) per severity — single-sourced from the editor mode
    // palette: green = went well, red = stop/wrong, calm blue = in progress.
    readonly property color _accent: {
        switch (root.severity) {
        case "success": return Theme.color.mode.insert;   // green "go"
        case "running": return Theme.color.mode.command;  // calm blue "working"
        default: return Theme.color.mode.replace;          // red "stop" (alert/error)
        }
    }

    // Nerd-font glyph per severity (rendered in editorFontFamily — see body).
    // Literal glyphs (the codebase convention): U+F00C check, U+F110 spinner,
    // U+F057 times-circle, U+F071 triangle.
    readonly property string _glyph: {
        switch (root.severity) {
        case "success": return "";  // nf-fa-check
        case "running": return "";  // nf-fa-spinner (spun while running)
        case "error": return "";    // nf-fa-times_circle
        default: return "";          // nf-fa-exclamation_triangle (alert)
        }
    }

    // Animation/visibility driver. show() raises it; the timer (or a fresh
    // show()) lowers it. `visible` rides the panel's fade so it keeps painting
    // through the exit and drops out once fully transparent. Driven off this
    // plain bool — NOT off `visible` — to avoid the enabled-binding race the
    // ModalOverlay header documents.
    property bool shown: false
    visible: panel.opacity > 0

    function show(toastTitle, toastDetail, toastSeverity) {
        root.title = toastTitle;
        root.detail = toastDetail;
        // Default keeps the 2-arg spawn-failure callers on the red "alert" look.
        root.severity = toastSeverity || "alert";
        root.shown = true;
        // Persistent severities (running/error) carry no timer; the rest
        // auto-hide. Stop any timer left armed by a prior auto-hide toast.
        if (root._isPersistent)
            hideTimer.stop();
        else
            hideTimer.restart();
    }

    function hide() {
        root.shown = false;
    }

    // Leftover spin from a prior "running" toast would tilt the next glyph
    // (a sideways check / triangle), so snap rotation back to upright whenever
    // we're not actively spinning.
    onSeverityChanged: if (root.severity !== "running") glyph.rotation = 0;

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

            // Severity glyph in the EDITOR font — editorFontFamily carries the
            // nerd glyphs; the chrome font may not. Glyph + accent track
            // `severity` (red triangle/times = wrong, green check = ok, blue
            // spinner = working), single-sourced from the editor mode palette.
            Text {
                id: glyph
                text: root._glyph
                color: root._accent
                font.family: editorFontFamily
                font.pixelSize: Theme.font.size.md
                renderType: Text.NativeRendering

                // Spin only while "running" — the visible "we are doing
                // something" cue. transformOrigin Center so it rotates in
                // place; root.onSeverityChanged snaps rotation back to 0 on
                // exit so the next glyph never renders tilted.
                transformOrigin: Item.Center
                RotationAnimator on rotation {
                    running: root.severity === "running"
                    loops: Animation.Infinite
                    from: 0
                    to: 360
                    duration: 900
                }
            }

            Column {
                // Remaining width after the glyph + Row spacing → both text
                // lines then wrap within the fixed-width card.
                width: body.width - glyph.width - body.spacing
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
                    visible: root.detail.length > 0
                    width: parent.width
                    text: root.detail
                    color: Theme.color.text.normal
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    wrapMode: Text.WordWrap
                    renderType: Text.NativeRendering
                }

                // Dismiss hint — ONLY on a persistent error (the one severity
                // that never auto-hides), so the user knows it is theirs to
                // clear. Click the card, Esc on the git surface, or run another
                // op all dismiss it.
                Text {
                    visible: root.severity === "error"
                    width: parent.width
                    text: "Esc or click to dismiss"
                    color: Theme.color.text.dim
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    renderType: Text.NativeRendering
                }
            }
        }

        // Click-to-dismiss — the SINGLE exception to the toast's
        // clicks-fall-through rule, enabled ONLY for a persistent error (a
        // disabled MouseArea is transparent to events, so every other
        // severity keeps falling through). Last panel child = top of the
        // stack, so it covers the card.
        MouseArea {
            anchors.fill: parent
            enabled: root.severity === "error"
            cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: root.hide()
        }
    }
}
