// Full-window agent surface — stream-json event log + composer.
//
// Toggled on/off by `controller.agentVisible`. When visible, the
// pane replaces the editor entirely (see Main.qml's `Item { NvimView
// | AgentPane }` swap). Not a side panel — the user's stated
// direction is that the Claude workflow takes over the whole window
// once entered, and the StatusBar below stays as a thin continuity
// strip across both modes.
//
// Layout: events ListView on top (fills available vertical space),
// composer TextField pinned at the bottom. Placeholder delegates
// render one row per stream-json event; turn grouping + tool-call
// drill-in still deferred to a follow-up iteration.
//
// Focus routing:
//   - Composer grabs focus whenever the pane becomes visible (no
//     mouse required; the user lands in the input ready to type).
//   - Escape in the composer calls `controller.hide_agent()` —
//     Main.qml's `onVisibleChanged` handler then returns focus to
//     the NvimView.
//   - Enter submits via `controller.submit_prompt(text)` and clears
//     the field. `controller.submit_prompt` spawns `claude -p`
//     (placeholder one-shot flow); the event log accumulates across
//     submissions so the pane reads as a running history.
//
// All colour and typography values bind against the `Theme`
// singleton (`qml/design/Theme.qml`). `Theme.color.agent` is the
// dedicated rung; system / result / rate-limit rows borrow
// `Theme.color.text.dim` intentionally. Adding new tokens lands in
// Theme.qml with a provenance comment first — no literals here.

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

import "design"

Rectangle {
    id: root

    color: Theme.color.bg.chrome

    // Generous outer padding — full-window mode earns more breathing
    // room than the previous side-panel did. Still rhythm-tied to
    // Theme.spacing so the whole chrome scales uniformly when the
    // token rungs are adjusted.
    property int horizontalPadding: Theme.spacing.lg
    property int verticalPadding: Theme.spacing.md

    // Composer footer sizing. Two rungs up from StatusBar so the
    // TextField has enough room for the caret + two lines of glance
    // text without feeling cramped.
    property int composerHeight: Theme.size.statusBarHeight * 2

    // Whenever the pane becomes visible, hand focus to the composer
    // directly. No intermediate mouse step, matches non-negotiable #1.
    onVisibleChanged: if (visible) composer.forceActiveFocus()

    ColumnLayout {
        anchors.fill: root
        anchors.leftMargin: root.horizontalPadding
        anchors.rightMargin: root.horizontalPadding
        anchors.topMargin: root.verticalPadding
        anchors.bottomMargin: root.verticalPadding
        spacing: Theme.spacing.md

        // --- Event log ----------------------------------------------
        ListView {
            id: events

            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true

            model: sessionModel
            spacing: Theme.spacing.md

            // Reusable delegates keep long streams smooth (§3 P1:
            // Repeater would pre-instantiate every event).
            reuseItems: true
            cacheBuffer: 300

            // Always auto-stick to bottom while new events arrive.
            // Scroll-position memory is a follow-up — during the
            // placeholder the reader is typically watching the
            // stream land in real time, not navigating backwards.
            onCountChanged: {
                if (events.count > 0) {
                    events.positionViewAtIndex(events.count - 1, ListView.End)
                }
            }

            delegate: Item {
                id: entry

                // Typed role injection — §3 P1. Static-checked by
                // qmllint, survives context-property changes.
                required property string kind
                required property string role
                required property string text
                required property bool partial
                required property string subtype

                width: events.width
                implicitHeight: body.implicitHeight

                Column {
                    id: body
                    width: entry.width
                    spacing: Theme.spacing.xs

                    Text {
                        id: roleLabel

                        visible: entry.role !== ""
                        text: _formatRoleLabel(entry.role, entry.subtype, entry.kind)
                        color: _roleColor(entry.role)
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        font.weight: Theme.font.weight.medium
                        font.letterSpacing: 0.6
                        renderType: Text.NativeRendering
                    }

                    Text {
                        id: bodyText

                        visible: entry.text !== ""
                        width: entry.width
                        text: _formatBody(entry.role, entry.kind, entry.text)
                        color: entry.partial
                            ? Theme.color.text.dim
                            : _bodyColor(entry.role)
                        wrapMode: Text.Wrap
                        textFormat: Text.PlainText
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        renderType: Text.NativeRendering
                    }
                }

                function _roleColor(r) {
                    if (r === "user") return Theme.color.agent.user
                    if (r === "assistant") return Theme.color.agent.assistant
                    return Theme.color.text.dim
                }

                function _bodyColor(r) {
                    if (r === "user") return Theme.color.text.strong
                    if (r === "assistant") return Theme.color.text.emphasis
                    return Theme.color.text.dim
                }

                function _formatRoleLabel(r, s, k) {
                    if (r === "user") return "You"
                    if (r === "assistant") return "Claude"
                    if (r === "system") {
                        if (k === "result") return "Result"
                        if (k === "rate_limit_event") return "Rate limit"
                        return s !== "" ? s : "System"
                    }
                    return k
                }

                function _formatBody(r, k, t) {
                    if (r === "" && k !== "") {
                        return "[" + k + "]" + (t !== "" ? " " + t : "")
                    }
                    return t
                }
            }

            // Empty-state affordance. Replaces the placeholder's
            // "set env var to populate" hint — the composer is the
            // new populate mechanism, so the empty state guides the
            // user towards it instead.
            Text {
                visible: events.count === 0
                anchors.centerIn: events
                horizontalAlignment: Text.AlignHCenter
                text: "type a prompt below and press Enter"
                color: Theme.color.text.dim
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.sm
                renderType: Text.NativeRendering
            }
        }

        // --- Composer footer ---------------------------------------
        Rectangle {
            id: composerFrame
            Layout.fillWidth: true
            Layout.preferredHeight: root.composerHeight
            color: Theme.color.bg.selected
            radius: Theme.radius.sm
            border.color: composer.activeFocus
                ? Theme.color.agent.assistant
                : Theme.color.border.hairline
            border.width: 1

            TextField {
                id: composer

                anchors.fill: composerFrame
                anchors.leftMargin: Theme.spacing.md
                anchors.rightMargin: Theme.spacing.md
                anchors.topMargin: Theme.spacing.sm
                anchors.bottomMargin: Theme.spacing.sm

                placeholderText: "message Claude — Enter to send, Esc to return to editor"
                placeholderTextColor: Theme.color.text.dim
                color: Theme.color.text.emphasis
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.md
                // Transparent bg — the parent Rectangle already paints
                // the composer chrome. Avoids the double-frame look
                // TextField ships with by default.
                background: Item {}
                selectByMouse: true
                renderType: Text.NativeRendering

                // Enter submits + clears. `controller.submit_prompt`
                // trims whitespace + no-ops on empty strings, so this
                // stays safe on stray enters.
                onAccepted: {
                    if (composer.text.length > 0) {
                        controller.submit_prompt(composer.text)
                        composer.text = ""
                    }
                }

                // Escape returns to the editor. Main.qml watches
                // `NvimView.onVisibleChanged` to restore focus on the
                // far side, so no additional focus handling needed here.
                Keys.onEscapePressed: controller.hide_agent()
            }
        }
    }
}
