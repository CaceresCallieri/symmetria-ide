// Always-on agent dock — top status bar mirroring StatusBar.qml at the
// bottom. Surfaces the multi-instance pool topology so the user can see
// every running agent at all times, even while focused on the editor or
// reading a file. Ctrl+1..5 in the agent pane switches focus between the
// agents shown here; <leader>aN in the editor spawns a new one into the
// next free slot. Closing the focused slot with <C-S-q> dims its bubble
// here in the same frame.
//
// Visibility:
//   Always visible. Chrome height stays constant (`Theme.size.statusBarHeight`)
//   so the editor viewport doesn't jump as instances spawn or close. The
//   chip strip is empty at launch (lazy-spawn: no slot is pre-warmed until
//   the user presses `<leader>aN` or an env-var startup path opts in).
//   An empty strip is intentional — the bar is always present as chrome
//   so layout doesn't shift, but chips only appear once the user asks for
//   an agent.
//
// Future surface (deferred — placeholder structure here):
//   - Per-agent working/idle state (animated when the SDK is awaiting a
//     response on that slot's session).
//   - Per-agent permission-mode indicator (today the pill lives inside
//     the agent pane; promotion to here would let the user see at a
//     glance which agent is in `bypassPermissions` etc.).
//   - Per-agent transcript-length glyph or last-tool-call summary.
//   The bubbles are deliberately quiet for now — adding richer per-slot
//   chrome belongs to a follow-up once turn grouping lands.
//
// All color and typography values bind against the `Theme` singleton
// (`qml/design/Theme.qml`). One-off pixel sizes (the 7px bubble) stay
// local per Theme.qml's own rule.

import QtQuick
import QtQuick.Layouts

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

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: Theme.spacing.md
        anchors.rightMargin: Theme.spacing.md
        spacing: Theme.spacing.md

        // Stretch fills the bar's left side so the bubble strip sits
        // anchored to the right edge — matches the user's original
        // preferred placement when the bubbles lived inside the agent
        // pane's chromeRow.
        Item { Layout.fillWidth: true }

        // --- Instance chip strip ------------------------------------
        //
        // One pill per ACTIVE pool slot — empty slots don't render (the
        // strip grows from right to left as the user spawns more agents,
        // matching the user's "no agents until I ask" mental model).
        // Visual grammar inherited from orchestrator.nvim's status_bar
        // module: number-only chip when no title, `<slot> │ <title>`
        // chip with a U+2502 thin vertical bar separator once the user
        // has sent a first message. Title is captured by Python from
        // `submit_prompt_for` and exposed via `controller.instanceTitles`
        // (per-slot list aligned to maxInstances).
        //
        // Two monochrome states — kept consistent with the user's earlier
        // "only black, no color system" preference; the focused chip
        // pops via brighter TEXT only, never a different fill colour:
        //
        //   focused → text.strong (brightest), bg.selected fill.
        //   active not focused → text.dim, bg.selected fill.
        //
        // Per PRD §3.2 the IDE never assigns per-instance colours; slot
        // POSITION + slot NUMBER are the differentiators.
        Row {
            id: instanceChips
            spacing: Theme.spacing.sm
            Layout.alignment: Qt.AlignVCenter

            Repeater {
                model: controller.activeInstanceSlots

                delegate: Rectangle {
                    id: chip

                    required property int index        // 0-based row in the Repeater
                    required property int modelData    // the actual slot number from activeInstanceSlots

                    readonly property int slot: chip.modelData
                    readonly property bool isFocused: controller.focusedInstance === chip.slot
                    readonly property string sessionTitle:
                        controller.instanceTitles[chip.slot - 1] || ""

                    // Pill geometry — radius = height / 2 keeps the cap
                    // truly round at any height. Width is implicit so the
                    // chip grows with title length up to the title's own
                    // 32-char Python-side cap; ElideRight on the inner
                    // Text gives us a final visual fallback if the chrome
                    // ever shrinks below the natural width.
                    height: Theme.size.modeBadgeHeight
                    radius: height / 2
                    color: Theme.color.bg.selected
                    implicitWidth: chipContent.implicitWidth + Theme.spacing.md * 2

                    Row {
                        id: chipContent
                        anchors.centerIn: parent
                        spacing: 0

                        Text {
                            id: slotNumber
                            text: chip.slot
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
                            visible: chip.sessionTitle !== ""
                            text: " │ "    // U+2502 thin vertical bar — matches orchestrator.nvim
                            color: Theme.color.text.dim
                            font.family: Theme.font.family
                            font.pixelSize: Theme.font.size.xs
                            renderType: Text.NativeRendering
                        }

                        Text {
                            id: titleText
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
                }
            }
        }
    }
}
