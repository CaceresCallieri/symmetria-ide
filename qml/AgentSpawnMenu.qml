// Keyboard-first agent menu (Ctrl+Shift+A — A-for-agent namespace).
//
// A centered modal panel. A single-letter key spawns a NEW / CONTINUE /
// RESUME session in the current HARNESS (the agent CLI — Claude or
// OpenCode). `o` toggles the harness, surfaced by the harness logo beside
// the title: the full Anthropic sunburst (Claude) or the OpenCode 3×3 grid,
// loaded as flat brand SVGs from the IDE's own asset tree (qml/assets/) and
// swapped live when `o` is pressed. The menu always re-opens on Claude, the
// daily-driver default.
//
// Permission polarity: spawns are DANGEROUS (skip-permissions) by default —
// the daily-driver polarity inherited from orchestrator.nvim's <leader>an
// family. One letter = go. Shift+letter still spawns the permission-checked
// variant, but that's a quiet power-user escape hatch and is intentionally
// NOT advertised in the menu anymore (the case distinction used to read as
// "o / O", "n / N", … — collapsed to a single letter per the simplify
// pass). Esc dismisses. No mouse interaction — chord-driven per the
// keyboard-first non-negotiable.
//
// Resume semantics differ per harness: claude's bare `-r` opens its own
// interactive picker inside the terminal; opencode's `--session` requires
// an id, so `r` on the OpenCode harness defers to the AgentSessionPicker
// overlay (wired in Main.qml via resumePickerRequested).
//
// All color and typography values bind against the `Theme` singleton.

import QtQuick

import "design"

Item {
    id: root

    anchors.fill: parent
    visible: false
    z: 40 // above the which-key overlay (z 20) and the focus hairline

    // The agent CLI the n/c/r keys spawn. Always resets to claude on
    // open() — harness choice is per-spawn, not sticky session state.
    property string harness: "claude"

    // Header brand-logo edge length. Sized just above the title text
    // (Theme.font.size.sm == 10) so the full Anthropic / OpenCode glyph
    // reads as a header badge without overpowering the title — 22 px was
    // too loud, 15 still a touch big, 13 sits right next to the label.
    readonly property int _headerIconSize: 13

    signal dismissed()
    // OpenCode resume needs a session id — Main.qml routes this to the
    // AgentSessionPicker overlay (carries the dangerous polarity the
    // user chose with the case of the `r` keypress).
    signal resumePickerRequested(bool dangerous)

    function open() {
        root.harness = "claude";
        root.visible = true;
        keyCatcher.forceActiveFocus();
    }

    // Re-grab modal focus WITHOUT resetting the chosen harness. Main.qml's
    // _restoreCentralFocus calls this on window re-activation (Alt-Tab away
    // and back) — calling open() there would clobber an OpenCode selection
    // back to Claude every time the user tabbed out. open()'s harness reset
    // is correct for a FRESH open, wrong for a re-assert. Mirrors
    // AgentSessionPicker.reassert()'s reason for existing.
    function reassert() {
        root.visible = true;
        keyCatcher.forceActiveFocus();
    }

    function dismiss() {
        root.visible = false;
        root.dismissed();
    }

    function _spawn(spawnType, dangerous) {
        // spawn_agent appends in display order, focuses the new agent
        // and switches the central surface, which lands keyboard focus
        // on the agent terminal — no explicit focus restore needed.
        root.visible = false;
        controller.spawn_agent(spawnType, dangerous, root.harness);
    }

    function _resume(dangerous) {
        if (root.harness === "opencode") {
            // No picker flag in the opencode CLI — hand off to the IDE's
            // session picker, which spawns `--session <id>` on accept.
            root.visible = false;
            root.resumePickerRequested(dangerous);
            return;
        }
        root._spawn("resume", dangerous);
    }

    // Entrance motion (FM-style scale-pop + opacity fade) is expressed as a
    // single root "shown" state + a to-only Transition, NOT as Behaviors on
    // visible-bound properties. A `to: "shown"` transition animates only the
    // way IN — leaving the state (dismiss) has no transition, so panel/scrim
    // snap straight to their hidden values. That (a) makes a rapid
    // open→dismiss→reopen always start the next pop from the full
    // popFromScale (no half-scaled, overshoot-less entrance), and (b) never
    // runs an exit animation against the already-hidden root Item. Only the
    // pop-IN is animated. (Behaviors with `enabled: root.visible` can't do
    // this safely — the enabled binding races the scale binding on open.)
    states: State {
        name: "shown"
        when: root.visible
        PropertyChanges { target: panel; scale: 1; opacity: 1 }
        PropertyChanges { target: scrim; opacity: 1 }
    }
    transitions: Transition {
        to: "shown"
        NumberAnimation {
            target: panel
            property: "scale"
            duration: Theme.anim.duration
            easing.type: Easing.OutBack
            easing.overshoot: Theme.anim.popOvershoot
        }
        NumberAnimation {
            targets: [panel, scrim]
            property: "opacity"
            duration: Theme.anim.duration
            easing.type: Easing.BezierSpline
            easing.bezierCurve: Theme.anim.standardCurve
        }
    }

    // Dim the surface behind the panel so the modal state is legible.
    // Hidden-state default; the "shown" state fades it in with the panel.
    Rectangle {
        id: scrim
        anchors.fill: parent
        color: Theme.color.bg.scrim
        opacity: 0
    }

    Rectangle {
        id: panel
        anchors.centerIn: parent
        width: 320
        implicitHeight: column.implicitHeight + Theme.spacing.lg * 2
        height: implicitHeight
        color: Theme.color.bg.chrome
        border.color: Theme.color.border.hairline
        border.width: 1
        radius: Theme.radius.lg

        // Hidden-state defaults — the root "shown" state animates these to
        // scale 1 / opacity 1 on open via the FM-style transition above.
        // transformOrigin Center so the pop grows from the panel's middle
        // (it's anchored centerIn parent).
        transformOrigin: Item.Center
        scale: Theme.anim.popFromScale
        opacity: 0

        Column {
            id: column
            anchors.fill: parent
            anchors.margins: Theme.spacing.lg
            spacing: Theme.spacing.sm

            // Header — harness logo + title, centered as a unit. The logo
            // is the full brand glyph for the current harness: the
            // Anthropic sunburst (Claude) or the OpenCode 3×3 grid. Source
            // swaps on `root.harness`, so pressing `o` flips the logo in
            // place. No arrow — the title is a plain "Spawn agent #N" label
            // (numbering is dense display order, so a new agent always
            // becomes #(count + 1)).
            Row {
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: Theme.spacing.sm

                Image {
                    anchors.verticalCenter: parent.verticalCenter
                    // Full brand logo, copied into the IDE's own asset tree
                    // from ~/.dotfiles/scripts/{claude,opencode}-icon.svg
                    // (the glyphs the desktop notification stack uses) so
                    // the repo carries its own copy — no runtime dependency
                    // on the user's dotfiles. Brand fills are baked into the
                    // SVGs (Claude #D97757 / OpenCode #6f9bd6) and identify
                    // the backend regardless of theme — intentionally NOT
                    // Theme-tokened. Path is relative to this QML file.
                    source: root.harness === "opencode"
                            ? "assets/opencode-icon.svg"
                            : "assets/claude-icon.svg"
                    // Rasterize the vector at 2× the display box so the
                    // glyph stays crisp at this small size.
                    sourceSize.width: root._headerIconSize * 2
                    sourceSize.height: root._headerIconSize * 2
                    width: root._headerIconSize
                    height: root._headerIconSize
                    smooth: true
                    fillMode: Image.PreserveAspectFit
                }

                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: "Spawn agent #" + (controller.agentOrder.length + 1)
                    color: Theme.color.text.strong
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    font.weight: Theme.font.weight.bold
                    renderType: Text.NativeRendering
                }
            }

            // Extra breathing room below the centered header, setting it
            // apart from the left-aligned key rows — the Column's uniform
            // sm gap alone read too tight at this seam.
            Item { width: 1; height: Theme.spacing.sm }

            // Action list — one single-letter key per row. Keys are single
            // monospace glyphs, so the label column self-aligns without a
            // fixed-width key cell. n/c/r spawn in the current harness; o
            // toggles the harness (its only behaviour — reflected by the
            // header icon above).
            Repeater {
                model: [
                    { key: "n", label: "new session" },
                    { key: "c", label: "continue" },
                    { key: "r", label: root.harness === "opencode"
                                       ? "resume (session picker)"
                                       : "resume (claude's picker)" },
                    { key: "o", label: "switch harness" },
                ]

                delegate: Row {
                    id: entryRow
                    required property var modelData
                    spacing: Theme.spacing.sm

                    Text {
                        text: entryRow.modelData.key
                        color: Theme.color.text.strong
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        font.weight: Theme.font.weight.medium
                        renderType: Text.NativeRendering
                    }
                    Text {
                        text: entryRow.modelData.label
                        color: Theme.color.text.normal
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        renderType: Text.NativeRendering
                    }
                }
            }

            // Matching breathing room above the footer, separating the
            // action list from the Esc hint (same intent as the spacer
            // above the list).
            Item { width: 1; height: Theme.spacing.sm }

            // The one surviving footer hint — the skip-permissions /
            // safe-mode explanation lines were removed in the simplify
            // pass (dangerous is now the unadvertised default).
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "Esc → cancel"
                color: Theme.color.text.dim
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.xs
                renderType: Text.NativeRendering
            }
        }
    }

    Item {
        id: keyCatcher
        // Modal key routing: while the menu is visible this item holds
        // active focus, so the chords below never leak to the surface
        // underneath.
        //
        // Self-heal: if anything steals active focus while the menu is
        // still visible (window re-activation dispatch, a terminal pane
        // re-grabbing focus after Alt-Tab), take it back on the next
        // event-loop tick. Without this the menu goes deaf — visible but
        // receiving no keys, with no way to dismiss it. Both close paths
        // (_spawn/dismiss) flip root.visible to false BEFORE focus moves
        // on, so this never fights a legitimate focus handoff.
        onActiveFocusChanged: {
            if (!activeFocus && root.visible)
                Qt.callLater(() => {
                    if (root.visible)
                        keyCatcher.forceActiveFocus();
                });
        }
        Keys.onPressed: function (event) {
            event.accepted = true;
            switch (event.key) {
            case Qt.Key_Escape:
                root.dismiss();
                break;
            case Qt.Key_O:
                root.harness = root.harness === "claude" ? "opencode" : "claude";
                break;
            case Qt.Key_N:
                root._spawn("fresh", !(event.modifiers & Qt.ShiftModifier));
                break;
            case Qt.Key_C:
                root._spawn("continue", !(event.modifiers & Qt.ShiftModifier));
                break;
            case Qt.Key_R:
                root._resume(!(event.modifiers & Qt.ShiftModifier));
                break;
            default:
                // Swallow everything else — modal.
                break;
            }
        }
    }
}
