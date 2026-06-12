// Keyboard-first agent menu (Ctrl+Shift+A — A-for-agent namespace).
//
// A centered modal panel: n/c/r spawn a NEW / CONTINUE / RESUME session
// in the selected HARNESS (the agent CLI — Claude or OpenCode; `o`
// toggles between them and the menu always re-opens on Claude, the
// daily-driver default). Lowercase keys spawn the DANGEROUS variant
// (skip-permissions — the daily-driver polarity inherited from
// orchestrator.nvim's <leader>an family); Shift+letter spawns the
// permission-checked variant. Esc dismisses. No mouse interaction in
// v1 — this is a chord-driven surface per the keyboard-first
// non-negotiable.
//
// Resume semantics differ per harness: claude's bare `-r` opens its own
// interactive picker inside the terminal; opencode's `--session`
// requires an id, so `r` on the OpenCode harness defers to the
// AgentSessionPicker overlay (wired in Main.qml via
// resumePickerRequested).
//
// Placeholder-discipline styling (minimal panel, Theme tokens only);
// richer treatment lands once the agent surface has real usage cadence.
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

    // Dim the surface behind the panel so the modal state is legible.
    Rectangle {
        anchors.fill: parent
        color: Theme.color.bg.scrim
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
        radius: Theme.radius.md

        Column {
            id: column
            anchors.fill: parent
            anchors.margins: Theme.spacing.lg
            spacing: Theme.spacing.sm

            Text {
                // Numbering is dense display order, so a new agent always
                // becomes #(count + 1) — name it so the user knows which
                // Ctrl+N will reach it afterwards.
                text: "Spawn agent → #" + (controller.agentOrder.length + 1)
                color: Theme.color.text.strong
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.sm
                font.weight: Theme.font.weight.bold
                renderType: Text.NativeRendering
            }

            Row {
                spacing: Theme.spacing.sm
                Text {
                    text: "o / O"
                    color: Theme.color.text.strong
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    font.weight: Theme.font.weight.medium
                    renderType: Text.NativeRendering
                }
                Text {
                    text: "harness: "
                          + (root.harness === "opencode" ? "OpenCode" : "Claude")
                    color: Theme.color.text.normal
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    renderType: Text.NativeRendering
                }
            }

            Repeater {
                model: [
                    { key: "n", label: "new session" },
                    { key: "c", label: "continue" },
                    { key: "r", label: root.harness === "opencode"
                                       ? "resume (session picker)"
                                       : "resume (claude's picker)" },
                ]

                delegate: Row {
                    id: entryRow
                    required property var modelData
                    spacing: Theme.spacing.sm

                    Text {
                        text: entryRow.modelData.key + " / " + entryRow.modelData.key.toUpperCase()
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

            // Footer hints — one per row: a single joined line overflows
            // the fixed panel width and gets clipped at the right edge.
            Column {
                spacing: Theme.spacing.xs
                Repeater {
                    model: [
                        "lowercase ⚠ skip-permissions",
                        "Shift+key → safe mode (ask permissions)",
                        "Esc → cancel",
                    ]
                    delegate: Text {
                        required property string modelData
                        text: modelData
                        color: Theme.color.text.dim
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        renderType: Text.NativeRendering
                    }
                }
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
