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
// The choice is binary but there are now THREE base rungs under this control,
// and `bg.selected` is tuned for the darker two. Measured step from the base
// to the active fill: over `canvas` 0x17, over `chrome` 0x13, over `bar` 0x0f
// — so the two bar-mounted switchers (the surface switcher and local|vps) get
// the weakest active-state signal of the three. If that ever reads too faint
// on a real seat, the fix is a THIRD value, not a brighter `bg.selected`:
// raising the shared token would over-drive the git surface's consumers,
// which sit on the darker rungs and already read correctly.
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

// `ComponentBehavior: Bound` binds each component to the context it is DEFINED
// in, rather than letting it pick up the context it is instantiated in — which
// is what lets the linter resolve the outer `root` id from inside the
// Repeater's delegate. The read already worked at runtime either way; only the
// static resolution changes. Without the pragma every `root.*` read inside the
// delegate is reported `unqualified` — nine of them here — because qmllint
// will not resolve an outer id across a Component boundary. An earlier comment
// in this file asserted those nine were unfixable short of injecting a
// `required property` per read; that was wrong, and the linter's own hint
// names this pragma as the remedy. Safe here because the delegate already
// declares `required property var modelData` rather than relying on the
// implicit context property, which is the one thing the pragma takes away.
//
// ⚠ Note the wording above: a comment line whose FIRST word after `//` is the
// linter's name is parsed as a lint directive, and every following word is
// read as a diagnostic category. Writing that name at the start of a line here
// turned this paragraph into twelve `invalid-lint-directive` findings.
pragma ComponentBehavior: Bound

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

    // The family the icon glyphs render in. Declared here rather than reading
    // the `editorFontFamily` context property inside the delegate, so that a
    // SHARED component states its own dependency instead of requiring whatever
    // engine instantiates it to have set a particular Python context property.
    // `Theme.font.family` IS `editorFontFamily` (Theme.qml resolves it), so
    // this is the same font by a declared route, and a consumer can override.
    //
    property string iconFontFamily: Theme.font.family

    // Does `current` name a segment that actually exists here? Normally yes,
    // but `centralSurface` has a FIFTH value, "browser", that the surface
    // switcher deliberately carries no segment for. With no match, every
    // segment would be non-current — so an icon-bearing control would render
    // as a row of bare glyphs with no label and no highlight, saying nothing
    // at all. Falling back to "label everything" there restores what the
    // fully-labelled control used to show in that state.
    //
    // Written as a binding BLOCK, not `segments.some(...)`: a function call in
    // a binding does not re-evaluate (gotcha #3), and `.some` additionally
    // assumes a real JS array, which a QVariantList arriving from Python is
    // not. The loop reads `root.segments` and `root.current` directly, so it
    // re-evaluates when either changes and works for both shapes.
    readonly property bool hasCurrentSegment: {
        const list = root.segments;
        if (!list)
            return false;
        for (let i = 0; i < list.length; ++i) {
            if (list[i].key === root.current)
                return true;
        }
        return false;
    }

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
            // `label` is documented as required, but an absent one would assign
            // `undefined` to Text.text (runtime warning) and a present-but-empty
            // one would leave a zero-width VISIBLE Text, which still earns the
            // Row's spacing and pads the pill with nothing.
            readonly property string labelText: segment.modelData.label || ""
            // No icon means the label is the only content, so it must always
            // draw. With an icon, the label is the CURRENT segment's alone --
            // unless nothing is current at all, when every label returns (see
            // `hasCurrentSegment`).
            readonly property bool showLabel:
                segment.iconGlyph === "" || segment.isCurrent || !root.hasCurrentSegment

            height: root.segmentHeight
            implicitWidth: segmentContent.implicitWidth + root.horizontalPadding * 2

            // Only the icon-bearing controls ever change width, so only they
            // pay for the ease and the clip -- a scissor node per delegate is
            // wasted on a control whose `showLabel` is constant. (In
            // GitHistoryView it would be wasted twice: its `segments` array is
            // rebuilt whenever a count changes, which recreates the delegates,
            // so a Behavior there could never run to begin with.)
            clip: segment.iconGlyph !== ""
            Behavior on implicitWidth {
                enabled: segment.iconGlyph !== ""
                NumberAnimation {
                    duration: Theme.anim.quick
                    easing.type: Easing.Bezier
                    easing.bezierCurve: Theme.anim.standardCurve
                }
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
            // LEADING-edge anchored, not centred. Centring looks equivalent at
            // rest (the pill is exactly content + 2x padding) but breaks during
            // the width ease: the content jumps to its full implicit width in
            // one frame while the pill eases up behind it, so a CENTRED row
            // overflows on BOTH sides and the clip cuts the icon's left edge
            // for the length of the animation. Anchored left, the overflow is
            // entirely on the trailing edge, which is what makes the clip read
            // as the label being revealed rather than as the icon being eaten.
            Row {
                id: segmentContent
                anchors.left: parent.left
                anchors.leftMargin: root.horizontalPadding
                anchors.verticalCenter: parent.verticalCenter
                spacing: Theme.spacing.xs

                Text {
                    id: segmentIcon
                    anchors.verticalCenter: parent.verticalCenter
                    visible: segment.iconGlyph !== ""
                    text: segment.iconGlyph
                    // The glyphs are private-use-area codepoints, so this must
                    // stay a Nerd Font family or every mark renders as a tofu
                    // box. `iconFontFamily` defaults to `Theme.font.family`,
                    // which IS the resolved Nerd Font -- an earlier version of
                    // this comment claimed the two differed and named that as
                    // the reason for reaching past Theme, which was simply
                    // wrong (see Theme.qml's typography block).
                    font.family: root.iconFontFamily
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
                    visible: segment.showLabel && segment.labelText !== ""
                    text: segment.labelText
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
