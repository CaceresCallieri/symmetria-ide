// Native agent pane — flat stream-json event renderer (placeholder).
//
// Sibling of NvimView inside Main.qml's RowLayout. Renders one row
// per stream-json event published by `sessionModel` (SessionModel in
// `src/symmetria_ide/session_models.py`). Turn grouping and tool-
// call drill-in are deliberately out of scope for the placeholder;
// the point of this surface is to expose the real event vocabulary
// with minimal design commitment so follow-up iterations can
// iterate against actual data.
//
// Focus stays on NvimView — this pane is a passive display during
// the placeholder spike. Keystroke routing and a proper focus-
// switch affordance land with the composer.
//
// All colour and typography values bind against the `Theme`
// singleton (`qml/design/Theme.qml`). Local palette / typography
// literals belong in Theme, not here. Theme.color.agent is the
// dedicated rung for this pane (added by the preceding commit);
// system / result / rate-limit rows borrow `Theme.color.text.dim`
// intentionally rather than getting their own rungs.

import QtQuick

import "design"

Rectangle {
    id: root

    color: Theme.color.bg.chrome

    // Hairline divider against the editor on the left. Matches the
    // StatusBar divider in weight + color so both dividers read as
    // one cohesive chrome line.
    Rectangle {
        width: 1
        height: root.height
        color: Theme.color.border.hairline
        anchors.left: root.left
    }

    // Inner padding budget. Chrome-wide rhythm is driven by Theme.spacing;
    // sm horizontal, md vertical keeps the pane visually calm without
    // squeezing the streaming text against the divider.
    property int horizontalPadding: Theme.spacing.md
    property int verticalPadding: Theme.spacing.md

    ListView {
        id: events

        anchors.fill: root
        anchors.leftMargin: root.horizontalPadding
        anchors.rightMargin: root.horizontalPadding
        anchors.topMargin: root.verticalPadding
        anchors.bottomMargin: root.verticalPadding

        model: sessionModel
        spacing: Theme.spacing.md

        // ListView recycling (§3 P1). Rows can number into the
        // hundreds during a long streaming session — Repeater would
        // instantiate every delegate up front and the scrolling feel
        // would degrade. Reusable delegates + a small cache buffer
        // gives smooth scroll without pre-instantiating off-screen
        // content.
        reuseItems: true
        cacheBuffer: 200

        // Auto-stick to bottom while new events append. Scroll-
        // position memory (do-not-autoscroll-if-user-scrolled-up) is
        // a follow-up concern; the placeholder always sticks to
        // bottom because the reader is typically watching the
        // stream arrive in real time, not navigating backwards.
        onCountChanged: {
            if (events.count > 0) {
                events.positionViewAtIndex(events.count - 1, ListView.End)
            }
        }

        delegate: Item {
            id: entry

            // Required role properties — §3 P1: typed injection, static-
            // checked by qmllint, survives context-property changes.
            required property string kind
            required property string role
            required property string text
            required property bool partial
            required property string subtype

            // Row width matches the ListView's content width (already
            // inset by the outer pane's margins). Height follows the
            // inner Column's natural height plus a small bottom
            // breathing gap handled by ListView.spacing.
            width: events.width
            implicitHeight: body.implicitHeight

            Column {
                id: body
                width: entry.width
                spacing: Theme.spacing.xs

                // Role label. Tiny, slightly muted — this is a
                // discoverability affordance, not primary content.
                // Hidden for empty-role rows (unknown event kinds)
                // because the body already conveys the discriminator
                // via the kind tag at the line prefix.
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

                // Body text. Wraps for long assistant responses. Dim
                // while partial (streaming) so the reader can spot
                // in-flight vs finalised turns at a glance; normal
                // weight once the finalised event arrives.
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

            // Pure functions — no state, no signals — so the
            // delegate stays declarative. Defined inside the
            // delegate so they capture nothing from the outer
            // ListView scope beyond the `Theme` singleton.
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
                    // `subtype` carries the discriminator for system
                    // rows (init, hook_*, rate-limit statuses). `kind`
                    // supplements it for result rows whose subtype is
                    // e.g. "success" rather than "result".
                    if (k === "result") return "Result"
                    if (k === "rate_limit_event") return "Rate limit"
                    return s !== "" ? s : "System"
                }
                return k
            }

            function _formatBody(r, k, t) {
                // Unknown-kind rows: surface the kind as a prefix so
                // a new protocol envelope shows up visibly rather
                // than rendering an empty row that reads as a
                // silent drop.
                if (r === "" && k !== "") {
                    return "[" + k + "]" + (t !== "" ? " " + t : "")
                }
                return t
            }
        }

        // Empty-state affordance. Plain text centered in the pane
        // when no events have arrived yet — typically because
        // SYMMETRIA_IDE_AGENT_PROMPT wasn't set and no subprocess
        // was spawned. Helps the pane read as "empty by design"
        // rather than "something is broken".
        Text {
            visible: events.count === 0
            anchors.centerIn: events
            text: "agent pane — set SYMMETRIA_IDE_AGENT_PROMPT to populate"
            color: Theme.color.text.dim
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.xs
            renderType: Text.NativeRendering
        }
    }
}
