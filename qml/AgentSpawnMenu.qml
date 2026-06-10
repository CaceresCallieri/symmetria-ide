// Keyboard-first agent menu (Ctrl+Shift+A — A-for-agent namespace).
//
// A centered modal panel: n/c/r spawn a NEW / CONTINUE / RESUME Claude
// session. Lowercase keys spawn the DANGEROUS variant
// (--dangerously-skip-permissions — the daily-driver polarity inherited
// from orchestrator.nvim's <leader>an family); Shift+letter spawns the
// permission-checked variant. Esc dismisses. Future backends extend the
// key set (o = OpenCode) instead of burning new chords. No mouse
// interaction in v1 — this is a chord-driven surface per the
// keyboard-first non-negotiable.
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

    signal dismissed()

    function open() {
        root.visible = true;
        keyCatcher.forceActiveFocus();
    }

    function dismiss() {
        root.visible = false;
        root.dismissed();
    }

    function _spawn(spawnType, dangerous) {
        // spawn_agent focuses the new slot and switches the central
        // surface, which lands keyboard focus on the agent terminal —
        // no explicit focus restore needed on this path.
        root.visible = false;
        controller.spawn_agent(spawnType, dangerous);
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
                text: "Spawn agent"
                color: Theme.color.text.strong
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.sm
                font.weight: Theme.font.weight.bold
            }

            Repeater {
                model: [
                    { key: "n", label: "new session" },
                    { key: "c", label: "continue" },
                    { key: "r", label: "resume (claude's picker)" },
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
                    }
                    Text {
                        text: entryRow.modelData.label
                        color: Theme.color.text.normal
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                    }
                }
            }

            Text {
                text: "lowercase ⚠ skip-permissions · Shift+key safe · Esc cancel"
                color: Theme.color.text.dim
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.xs
            }
        }
    }

    Item {
        id: keyCatcher
        // Modal key routing: while the menu is visible this item holds
        // active focus, so the chords below never leak to the surface
        // underneath.
        Keys.onPressed: function (event) {
            event.accepted = true;
            switch (event.key) {
            case Qt.Key_Escape:
                root.dismiss();
                break;
            case Qt.Key_N:
                root._spawn("fresh", !(event.modifiers & Qt.ShiftModifier));
                break;
            case Qt.Key_C:
                root._spawn("continue", !(event.modifiers & Qt.ShiftModifier));
                break;
            case Qt.Key_R:
                root._spawn("resume", !(event.modifiers & Qt.ShiftModifier));
                break;
            default:
                // Swallow everything else — modal.
                break;
            }
        }
    }
}
