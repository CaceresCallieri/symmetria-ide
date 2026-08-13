// Flat segmented control — the IDE's ONE two-or-more-way switcher.
//
// Four surfaces hand-rolled this same control before it existed: AgentTopBar's
// location toggle (local|vps) and surface switcher (terminal|editor|agents|git),
// GitStatusPanel's changes-scope switcher (all|this agent), and GitHistoryView's
// tab header (changes|history|PRs). They were copies of each other down to the
// letter spacing, which is why the IDE read as having more toggles than it has
// ideas — five identical-looking controls in unrelated contexts.
//
// THE STATE GRAMMAR (what replaced the claymorphism):
//   active   — filled `bg.selected` + hairline border + `text.strong`, bold
//   inactive — transparent fill AND border, `text.dim`, medium weight
// Before the flat-aesthetic move the active segment was raised into an extruded
// clay capsule and the fill did secondary work. With the depth gone the fill,
// the border and the text weight carry the whole signal, which is why all three
// move together rather than just the colour.
//
// `onRaisedSurface` picks the selected rung. Inside a PillCard (a modal, a
// picker, a detail card) the surface is already `bg.raised`, so the chrome-level
// `bg.selected` would sit only a few lightness units above its own background
// and the active segment would read at about half strength. Set it there.
//
// Keyboard is NOT this component's business. Every one of these switchers has a
// chord that is the primary path (Ctrl+Shift+E/T/A, Ctrl+Shift+U, Ctrl+Shift+D,
// Tab) and the click is the convenience twin — a non-negotiable of the IDE, so
// a consumer that adds a segment here must also give it a key.
//
// ICONS AND THE ACTIVE-ONLY LABEL. A segment may carry an optional `icon` (a
// Nerd Font glyph, from `Theme.glyph.*`). When it does, the icon draws always
// and the label draws ONLY while that segment is current — so the control
// costs one word plus N-1 glyphs instead of N words. Segments without an
// `icon` keep the plain always-labelled rendering, which is what the narrow
// two-way switchers still want: a lone glyph has to be guessable, and "all"
// vs "this agent" has no icon anybody would read correctly.
//
// The cost is real and deliberate: the active segment is WIDER than the rest,
// so switching re-flows every segment after it and the glyphs do not hold
// fixed screen positions. Accepted because the chords are the primary path
// and clicking is the twin; `Behavior on implicitWidth` + `clip` turn the
// re-flow into a reveal rather than a jump. If position memory ever matters
// more than width, the fix is a fixed per-segment width, not a wider label.
//
// `segments` is an array of `{key, label, icon?}`. Compose a dynamic label (a count, a
// current ref) into `label` at the CALL SITE rather than passing a formatting
// function: a function call in a QML binding does not re-evaluate (CLAUDE.md
// gotcha #3), so a `labelFor(key)` property would silently freeze the first
// value it computed. Rebuilding the array re-creates the delegates, which is
// free here — they hold no state and back no live process. (That is the exact
// opposite of the agent/terminal pane Repeaters, where delegate churn kills a
// running process; the rule there does not transfer to three Text items.)

import QtQuick

import "design"

Row {
    id: root

    // [{key: string, label: string, icon?: string}] — see the header note on
    // `icon` and the active-only label.
    property var segments: []

    // The `key` of the active segment.
    property string current: ""

    // Set on any consumer sitting inside a PillCard — see the header note.
    property bool onRaisedSurface: false

    property int segmentHeight: Theme.size.modeBadgeHeight
    property int horizontalPadding: Theme.spacing.md
    // Not a capsule (`height / 2`) any more. A generous corner is what made an
    // extruded clay chip read as a physical object; on a flat fill the same
    // shape reads as a dated pill, and the corner is the last thing carrying
    // the old look. `md` keeps it soft without rounding into a lozenge.
    property int segmentRadius: Theme.radius.md

    signal activated(string key)

    spacing: Theme.spacing.sm

    Repeater {
        model: root.segments

        delegate: Item {
            id: segment

            required property var modelData
            readonly property bool isCurrent: root.current === segment.modelData.key
            // Absent `icon` reads as undefined; coerce to "" so the two
            // renderings below are a plain string test rather than a
            // truthiness test on a possibly-missing key.
            readonly property string iconGlyph: segment.modelData.icon || ""
            // No icon means the label is the only content, so it must always
            // draw. With an icon, the label is the CURRENT segment's alone.
            readonly property bool showLabel:
                segment.iconGlyph === "" || segment.isCurrent

            height: root.segmentHeight
            implicitWidth: segmentContent.implicitWidth + root.horizontalPadding * 2

            // The label appearing/disappearing changes this segment's width.
            // Ease it, and clip so the label is REVEALED by the growing pill
            // instead of spilling past its edge for the length of the ease.
            clip: true
            Behavior on implicitWidth {
                NumberAnimation { duration: Theme.anim.quick }
            }

            Rectangle {
                anchors.fill: parent
                radius: root.segmentRadius
                color: segment.isCurrent
                       ? (root.onRaisedSurface
                          ? Theme.color.bg.raisedSelected
                          : Theme.color.bg.selected)
                       : "transparent"
                border.width: 1
                border.color: segment.isCurrent
                              ? Theme.color.border.hairline
                              : "transparent"

                // Ease the fill/border swap so tabbing through does not snap.
                // Same `quick` rung the clay raise used, for continuity with
                // the rest of the chrome's state transitions.
                Behavior on color {
                    ColorAnimation { duration: Theme.anim.quick }
                }
                Behavior on border.color {
                    ColorAnimation { duration: Theme.anim.quick }
                }
            }

            // Row, not two anchored Texts: a positioner drops invisible
            // children from the layout AND drops the spacing that would
            // precede them, so the icon re-centres on its own the moment the
            // label hides. Hand-anchoring would need a width:0 dance.
            Row {
                id: segmentContent
                anchors.centerIn: parent
                spacing: Theme.spacing.xs

                Text {
                    id: segmentIcon
                    anchors.verticalCenter: parent.verticalCenter
                    visible: segment.iconGlyph !== ""
                    text: segment.iconGlyph
                    // editorFontFamily, not Theme.font.family: the chrome UI
                    // font has no private-use-area glyphs, so the mark would
                    // render as a tofu box. Same reason the agent chips'
                    // globe and worktree marks take this family.
                    font.family: editorFontFamily
                    // A rung above the label. Icons carry their weight over an
                    // area rather than a stem, so matching the label's 9px
                    // makes them read smaller than the text beside them.
                    font.pixelSize: Theme.font.size.md
                    color: segment.isCurrent ? Theme.color.text.strong : Theme.color.text.dim
                    renderType: Text.NativeRendering

                    Behavior on color {
                        ColorAnimation { duration: Theme.anim.quick }
                    }
                }

                Text {
                    id: segmentLabel
                    anchors.verticalCenter: parent.verticalCenter
                    visible: segment.showLabel
                    text: segment.modelData.label
                    color: segment.isCurrent ? Theme.color.text.strong : Theme.color.text.dim
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    font.weight: segment.isCurrent ? Theme.font.weight.bold : Theme.font.weight.medium
                    font.letterSpacing: 0.6
                    renderType: Text.NativeRendering

                    Behavior on color {
                        ColorAnimation { duration: Theme.anim.quick }
                    }
                }
            }

            MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: root.activated(segment.modelData.key)
            }
        }
    }
}
