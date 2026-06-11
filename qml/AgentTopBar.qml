// Always-on agent dock — top status bar mirroring StatusBar.qml at the
// bottom. Surfaces the terminal-agent pool (the IDE-native orchestrator
// runtime) so the user can see every running agent at all times, even
// while focused on the editor or the shell. Ctrl+1..5 focuses a slot,
// Ctrl+Shift+A → n spawns into the next free one, Ctrl+Shift+Q closes the
// focused one — closing dims its bubble here in the same frame.
//
// Visibility:
//   Always visible. Chrome height stays constant (`Theme.size.statusBarHeight`)
//   so the central viewport doesn't jump as instances spawn or close. The
//   chip strip is empty at launch (lazy-spawn: nothing runs until the
//   user presses Ctrl+Shift+A → n or an env-var startup path opts in). An
//   empty strip is intentional — the bar is always present as chrome so
//   layout doesn't shift, but chips only appear once the user asks.
//
// Chip anatomy: shared-module AgentChip (the Symmetria Shell sparkle —
// Claude-orange starburst animating from hook-driven activity_state via
// the bridge subscription) + slot number + `│ <title>` once claude's OSC
// title lands. Focus is expressed through TEXT brightness only (strong
// vs dim) per the "no per-instance colour system" preference; the
// sparkle's brand orange is a backend identity, not a slot colour.
//
// STT props are bound inert (isSttTarget/sttIsTranscribing false) — STT
// targeting for IDE-native agents is deferred; the visuals are ready.
//
// All color and typography values bind against the `Theme` singleton
// (`qml/design/Theme.qml`). One-off pixel ratios (the 1.4× sparkle
// scale, inherited from the shell's cap-height convention) stay local.

import QtQuick
import QtQuick.Layouts
import Symmetria.Agents.UI as AgentsUI

import "design"

Rectangle {
    id: root
    color: Theme.color.bg.chrome

    // Hairline divider between the bar and the main content area below.
    // Mirrors StatusBar's top-anchored divider — together they bracket
    // the content with matched 1px chrome edges.
    Rectangle {
        width: root.width
        height: 1
        color: Theme.color.border.hairline
        anchors.bottom: root.bottom
    }

    // Surface switcher — terminal / editor / agents, pinned to the bar's
    // left edge (moved here from StatusBar's center per the 2026-06-11
    // layout decision). Anchored as a SIBLING of the centering RowLayout,
    // not a member: the chip strip must stay centered on the WHOLE bar,
    // and a layout member would shift that center by the switcher's
    // width. No overlap at sane widths — chips would need to span half
    // the bar minus the switcher before colliding; verify visually if
    // the pool grows past 5 or titles get long. Clicking is a
    // convenience; the chords (Ctrl+Shift+E toggle, Ctrl+1..5 agent
    // focus) remain primary.
    Row {
        id: surfaceSwitcher
        anchors.left: root.left
        anchors.leftMargin: Theme.spacing.md
        anchors.verticalCenter: root.verticalCenter
        spacing: Theme.spacing.sm
        z: 1

        Repeater {
            // `surface` is the controller's centralSurface value (the
            // wire name stays singular "agent"); `label` is display-only.
            model: [
                { surface: "terminal", label: "terminal" },
                { surface: "editor", label: "editor" },
                { surface: "agent", label: "agents" },
            ]

            delegate: Rectangle {
                id: segment

                required property var modelData
                readonly property bool isCurrent: controller.centralSurface === segment.modelData.surface

                height: Theme.size.modeBadgeHeight
                radius: height / 2
                color: segment.isCurrent ? Theme.color.bg.selected : "transparent"
                implicitWidth: segmentLabel.implicitWidth + Theme.spacing.md * 2

                Text {
                    id: segmentLabel
                    anchors.centerIn: parent
                    text: segment.modelData.label
                    color: segment.isCurrent ? Theme.color.text.strong : Theme.color.text.dim
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    font.weight: segment.isCurrent ? Theme.font.weight.bold : Theme.font.weight.medium
                    font.letterSpacing: 0.6
                    renderType: Text.NativeRendering
                }

                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: controller.set_central_surface(segment.modelData.surface)
                }
            }
        }
    }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: Theme.spacing.md
        anchors.rightMargin: Theme.spacing.md
        spacing: Theme.spacing.md

        // Symmetric stretches center the bubble strip in the bar
        // (space-around) — per the 2026-06-10 layout decision; the
        // earlier right-anchored placement was carried over from the
        // SDK pane's chromeRow and is retired.
        Item { Layout.fillWidth: true }

        // --- Instance chip strip ------------------------------------
        //
        // One pill per ACTIVE pool slot — empty slots don't render (the
        // strip grows from right to left as the user spawns more agents,
        // matching the "no agents until I ask" mental model).
        Row {
            id: instanceChips
            spacing: Theme.spacing.sm
            Layout.alignment: Qt.AlignVCenter

            Repeater {
                // Display order (dense, compacts on close) — the chip's
                // visible number is its POSITION (index + 1); modelData is
                // the frozen INTERNAL slot (baked into SYMMETRIA_AGENT_ID
                // + the bridge identity) used for state lookups + focus.
                model: controller.agentOrder

                delegate: Rectangle {
                    id: chip

                    required property int index        // 0-based display position
                    required property int modelData    // internal pool slot

                    readonly property int slot: chip.modelData
                    readonly property int displayNumber: chip.index + 1
                    readonly property bool isFocused: controller.focusedAgent === chip.slot
                    readonly property string sessionTitle: controller.agentTitles[chip.slot - 1] || ""
                    readonly property var activity: controller.agentActivity[chip.slot - 1]

                    // Pill geometry — radius = height / 2 keeps the cap
                    // truly round at any height. Width is implicit so the
                    // chip grows with title length; ElideRight on the
                    // inner Text is the visual fallback if chrome shrinks.
                    height: Theme.size.modeBadgeHeight
                    radius: height / 2
                    color: Theme.color.bg.selected
                    implicitWidth: chipContent.implicitWidth + Theme.spacing.md * 2

                    Row {
                        id: chipContent
                        anchors.centerIn: parent
                        spacing: Theme.spacing.sm

                        // Shared sparkle (Symmetria.Agents.UI) — dormant
                        // dot when idle, starburst spin while the agent
                        // works, key/ask/plan morphs on permissions.
                        // Driven entirely by the bridge subscription feed.
                        AgentsUI.AgentChip {
                            anchors.verticalCenter: parent.verticalCenter
                            size: Theme.font.size.sm * 1.4
                            active: chip.isFocused
                            activityState: chip.activity ? chip.activity.state : ""
                            activityTool: chip.activity ? chip.activity.tool : ""
                            agentType: chip.activity ? chip.activity.agentType : "claude"
                            isSttTarget: false
                            sttIsTranscribing: false
                        }

                        Text {
                            id: slotNumber
                            anchors.verticalCenter: parent.verticalCenter
                            text: chip.displayNumber
                            color: chip.isFocused
                                ? Theme.color.text.strong
                                : Theme.color.text.dim
                            font.family: Theme.font.family
                            font.pixelSize: Theme.font.size.xs
                            font.weight: chip.isFocused
                                ? Theme.font.weight.bold
                                : Theme.font.weight.medium
                            font.letterSpacing: 0.6
                            renderType: Text.NativeRendering
                        }

                        Text {
                            id: titleSeparator
                            anchors.verticalCenter: parent.verticalCenter
                            visible: chip.sessionTitle !== ""
                            text: "│"    // U+2502 thin vertical bar — matches orchestrator.nvim
                            color: Theme.color.text.dim
                            font.family: Theme.font.family
                            font.pixelSize: Theme.font.size.xs
                            renderType: Text.NativeRendering
                        }

                        Text {
                            id: titleText
                            anchors.verticalCenter: parent.verticalCenter
                            visible: chip.sessionTitle !== ""
                            text: chip.sessionTitle
                            color: chip.isFocused
                                ? Theme.color.text.strong
                                : Theme.color.text.dim
                            font.family: Theme.font.family
                            font.pixelSize: Theme.font.size.xs
                            font.weight: Theme.font.weight.normal
                            renderType: Text.NativeRendering
                            elide: Text.ElideRight
                        }
                    }

                    // Click-to-focus convenience; keyboard (Ctrl+N) is primary.
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: controller.focus_agent(chip.slot)
                    }
                }
            }
        }

        // Right-side counterpart of the leading stretch — together they
        // center the strip.
        Item { Layout.fillWidth: true }
    }
}
