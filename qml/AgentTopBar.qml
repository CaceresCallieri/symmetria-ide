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
// STT props mirror controller.sttTargetSlot/sttTranscribing — the shell
// pushes its dictation target into the bridge hub, snapshots carry it as
// the "stt" field, and _mirror_stt_state (app.py) resolves it to one of
// our slots. Same bridge-only path the sparkle activity rides.
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

    // Location toggle — Local ↔ VPS context for THIS project. Renders only
    // when the pairing probe found the repo on a registered remote server
    // (controller.vpsAvailable), or defensively while location is already
    // "vps" so the way back never disappears. Pinned to the RIGHT edge
    // (user decision 2026-07-12 — it was briefly leftmost, which crowded
    // the surface switcher and muddled the two controls' reading order;
    // right-aligned it mirrors StatusBar's trailing ⇅ server badge, so
    // both location cues live on the right rail). Same clay-segment
    // anatomy as the switcher; Ctrl+Shift+U is the chord twin. Same
    // accepted-overlap caveat as the switcher's comment below, mirrored
    // to this edge: the centered chip strip could reach the toggle only
    // with many long-titled chips on a narrow window — verify visually
    // if that combination ever ships.
    Row {
        id: locationToggle
        anchors.right: root.right
        anchors.rightMargin: Theme.spacing.md
        anchors.verticalCenter: root.verticalCenter
        spacing: Theme.spacing.sm
        z: 1
        visible: controller.vpsAvailable || controller.location === "vps"

        Repeater {
            model: [
                { location: "local", label: "local" },
                { location: "vps", label: "vps" },
            ]

            delegate: Item {
                id: locationSegment

                required property var modelData
                readonly property bool isCurrent: controller.location === locationSegment.modelData.location

                height: Theme.size.modeBadgeHeight
                implicitWidth: locationSegmentLabel.implicitWidth + Theme.spacing.md * 2

                PillSurface {
                    anchors.fill: parent
                    radius: height / 2
                    elevated: locationSegment.isCurrent
                    color: locationSegment.isCurrent ? Theme.color.bg.selected : "transparent"
                    borderColor: locationSegment.isCurrent ? Theme.color.border.hairline : "transparent"
                }

                Text {
                    id: locationSegmentLabel
                    anchors.centerIn: parent
                    text: locationSegment.modelData.label
                    color: locationSegment.isCurrent ? Theme.color.text.strong : Theme.color.text.dim
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    font.weight: locationSegment.isCurrent ? Theme.font.weight.bold : Theme.font.weight.medium
                    font.letterSpacing: 0.6
                    renderType: Text.NativeRendering
                }

                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: controller.set_location(locationSegment.modelData.location)
                }
            }
        }
    }

    // Surface switcher — terminal / editor / agents, pinned at the bar's
    // left edge (moved here from StatusBar's center per the 2026-06-11
    // layout decision; the location toggle lives on the opposite edge so
    // the two controls never crowd each other). Anchored as a SIBLING of
    // the centering RowLayout,
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
                // "git" not "history": the surface is now dual-mode (a
                // Tab-toggled "changes" working-tree view + the commit log),
                // and it opens on changes — labelling it "history" would
                // mis-name where the chip lands.
                { surface: "git", label: "git" },
                // NB: no "browser" segment. The embedded browser is agent-owned
                // and reached only through its owning agent — click the globe on
                // that agent's chip (below) or press Ctrl+Shift+B with the agent
                // focused. Giving it a standalone switcher tab would contradict
                // that ownership model, so it was deliberately removed.
            ]

            delegate: Item {
                id: segment

                required property var modelData
                readonly property bool isCurrent: controller.centralSurface === segment.modelData.surface

                height: Theme.size.modeBadgeHeight
                implicitWidth: segmentLabel.implicitWidth + Theme.spacing.md * 2

                // The current surface raises into a clay capsule (matte fill +
                // hairline border + convex depth); inactive segments stay
                // fully flat — transparent fill AND border, no shadow — so
                // only the active surface reads as a raised chip. This is the
                // FM TabBar active-tab pattern; `elevated` gates the depth and
                // the whole transition eases via PillSurface's quick fades.
                PillSurface {
                    anchors.fill: parent
                    radius: height / 2
                    elevated: segment.isCurrent
                    color: segment.isCurrent ? Theme.color.bg.selected : "transparent"
                    borderColor: segment.isCurrent ? Theme.color.border.hairline : "transparent"
                }

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

                delegate: Item {
                    id: chip

                    required property int index        // 0-based display position
                    required property int modelData    // internal pool slot

                    readonly property int slot: chip.modelData
                    readonly property int displayNumber: chip.index + 1
                    // Two tiers of "focused": isFocusedSlot is the raw
                    // surface-agnostic focus state (controller.focusedAgent
                    // deliberately persists across surface swaps — it anchors
                    // Ctrl+Shift+A's go-then-spawn "back to where I was") and
                    // feeds the sparkle's `active` identity prop; isFocused
                    // additionally gates on the agent surface being the
                    // visible one and drives the text emphasis, so chips dim
                    // while the editor or terminal is on screen.
                    readonly property bool isFocusedSlot: controller.focusedAgent === chip.slot
                    readonly property bool isFocused: chip.isFocusedSlot && controller.centralSurface === "agent"
                    readonly property string sessionTitle: controller.agentTitles[chip.slot - 1] || ""
                    readonly property var activity: controller.agentActivity[chip.slot - 1]

                    // Pill geometry — radius = height / 2 keeps the cap
                    // truly round at any height. Width is implicit so the
                    // chip grows with title length; ElideRight on the
                    // inner Text is the visual fallback if chrome shrinks.
                    height: Theme.size.modeBadgeHeight
                    implicitWidth: chipContent.implicitWidth + Theme.spacing.md * 2

                    // Every bubble is a clay pill (matte fill + hairline
                    // border); the FOCUSED bubble additionally raises into
                    // full claymorphism (shadows + rim highlight) via
                    // `elevated`, so the active agent floats above the rest —
                    // depth reinforcing the existing text-brightness focus
                    // cue. Declared first (background) so the sparkle / number
                    // / title Row + the focus MouseArea paint on top untouched.
                    PillSurface {
                        anchors.fill: parent
                        radius: height / 2
                        elevated: chip.isFocused
                        color: Theme.color.bg.selected
                        borderColor: Theme.color.border.hairline
                    }

                    // Coordination attention dot — lit when a wait_for_agent
                    // trigger involving this agent needs the user (judge said
                    // needs_user, a wait was cancelled, or a message couldn't
                    // be delivered). Rides the CHIP's top-right corner, not the
                    // browser globe (its sibling attentionDot below requires
                    // browser ownership; coordination must show regardless).
                    // Blue (mode.command) so it reads "info/coordination",
                    // distinct from the globe badge's amber accent. Cleared by
                    // focusing the chip (focus_agent) — the plain-click
                    // MouseArea below already does that, so tapping the dot
                    // acknowledges it. agentCoordAttention is a PySide
                    // QVariantList — index then coerce, per
                    // qml_qvariantlist_array_check (no Array.isArray).
                    Rectangle {
                        id: coordAttentionDot
                        visible: !!(controller.agentCoordAttention[chip.slot - 1])
                        width: Math.round(chip.height * 0.28)
                        height: width
                        radius: width / 2
                        color: Theme.color.mode.command
                        border.width: 1
                        border.color: Theme.color.bg.chrome
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.rightMargin: -Math.round(width * 0.2)
                        anchors.topMargin: -Math.round(width * 0.2)
                        z: 2  // above chipContent (z:1) and the focus MouseArea

                        // Same gentle pulse discipline as the globe badge —
                        // alwaysRunToEnd so it never freezes mid-fade,
                        // Theme.anim durations (no hand-rolled ms).
                        SequentialAnimation {
                            running: coordAttentionDot.visible
                            loops: Animation.Infinite
                            alwaysRunToEnd: true
                            NumberAnimation {
                                target: coordAttentionDot; property: "opacity"
                                to: 0.45; duration: Theme.anim.duration
                                easing.type: Easing.InOutQuad
                            }
                            NumberAnimation {
                                target: coordAttentionDot; property: "opacity"
                                to: 1.0; duration: Theme.anim.duration
                                easing.type: Easing.InOutQuad
                            }
                        }
                    }

                    Row {
                        id: chipContent
                        anchors.centerIn: parent
                        spacing: Theme.spacing.sm
                        // Raised above the chip's full-fill focus MouseArea
                        // (declared later, default z) so the browser glyph's
                        // own MouseArea wins its clicks. The other children
                        // (sparkle, number, title) are plain non-interactive
                        // items, so clicks on them still fall through to the
                        // focus MouseArea below — focus-to-select keeps working.
                        z: 1

                        // Shared sparkle (Symmetria.Agents.UI) — dormant
                        // dot when idle, starburst spin while the agent
                        // works, key/ask/plan morphs on permissions.
                        // Driven entirely by the bridge subscription feed.
                        AgentsUI.AgentChip {
                            anchors.verticalCenter: parent.verticalCenter
                            size: Theme.font.size.sm * 1.4
                            // Surface-agnostic on purpose: `active` is the
                            // chip's focus identity, not visual emphasis.
                            // (Currently unconsumed inside AgentChip — the
                            // sparkle runs off activityState — but keep the
                            // semantics honest in case the shared module
                            // starts reading it.)
                            active: chip.isFocusedSlot
                            activityState: chip.activity ? chip.activity.state : ""
                            activityTool: chip.activity ? chip.activity.tool : ""
                            agentType: chip.activity ? chip.activity.agentType : "claude"
                            isSttTarget: controller.sttTargetSlot === chip.slot
                            sttIsTranscribing: controller.sttTranscribing
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

                        // Browser-link indicator — the PRIMARY way into the
                        // agent-owned browser (the standalone browser tab is
                        // gone). Shown when this agent owns ≥1 OPEN browser
                        // window (it opened one via browser_open). Clicking jumps
                        // to that window (newest-driven) — the mouse twin of the
                        // Ctrl+Shift+B keyboard jump. Two layers ride the glyph:
                        //   - the GLOBE = presence ("this agent has a browser");
                        //   - the DOT   = attention ("…and it wants you to look"),
                        //     lit by the agent's browser_request_attention call,
                        //     cleared when you view the window (focus_agent_browser).
                        // The dormant `active` pulse (in-flight driving) stays as
                        // the future-CDP-monitor hook but never fires today.
                        // Rendered in editorFontFamily (a Nerd Font) because the
                        // chip's UI font may lack icon glyphs. `agentBrowserCount`/
                        // `Active`/`Attention` are PySide QVariantLists — index
                        // then guard, per qml_qvariantlist_array_check (no
                        // Array.isArray).
                        Item {
                            id: browserIndicator
                            anchors.verticalCenter: parent.verticalCenter
                            readonly property bool owns:
                                (controller.agentBrowserCount[chip.slot - 1] || 0) > 0
                            readonly property bool active:
                                !!(controller.agentBrowserActive[chip.slot - 1])
                            readonly property bool attention:
                                !!(controller.agentBrowserAttention[chip.slot - 1])
                            visible: browserIndicator.owns
                            width: browserIndicator.visible ? browserGlyph.implicitWidth : 0
                            height: browserGlyph.implicitHeight

                            Text {
                                id: browserGlyph
                                anchors.centerIn: parent
                                text: ""  // nf-fa-globe — universal browser/web mark
                                font.family: editorFontFamily
                                font.pixelSize: Theme.font.size.sm
                                color: browserIndicator.active
                                    ? Theme.color.text.strong
                                    : Theme.color.text.dim
                                renderType: Text.NativeRendering

                                // Attention pulse only while actively driving;
                                // `alwaysRunToEnd` lets the loop finish on its
                                // second leg (opacity → 1.0) when activity ends,
                                // so the glyph never freezes mid-fade. Durations
                                // from Theme.anim (no hand-rolled ms) per the
                                // popup-animation convention.
                                SequentialAnimation {
                                    running: browserIndicator.active
                                    loops: Animation.Infinite
                                    alwaysRunToEnd: true
                                    NumberAnimation {
                                        target: browserGlyph; property: "opacity"
                                        to: 0.4; duration: Theme.anim.duration
                                        easing.type: Easing.InOutQuad
                                    }
                                    NumberAnimation {
                                        target: browserGlyph; property: "opacity"
                                        to: 1.0; duration: Theme.anim.duration
                                        easing.type: Easing.InOutQuad
                                    }
                                }
                            }

                            // Attention dot — the notification badge the agent
                            // raises via browser_request_attention. Accent fill
                            // with a chrome-bg ring so it reads as a badge ATOP
                            // the globe's top-right (the conventional notify
                            // corner). The 0.34 size + corner offsets are one-off
                            // pixel ratios (local, per the header convention).
                            // Drawn after the glyph so it paints on top; the
                            // shared MouseArea below still wins the click, so
                            // tapping the badge jumps + clears it.
                            Rectangle {
                                id: attentionDot
                                visible: browserIndicator.attention
                                width: Math.round(browserGlyph.implicitHeight * 0.34)
                                height: width
                                radius: width / 2
                                color: Theme.color.accent.primary
                                border.width: 1
                                border.color: Theme.color.bg.chrome
                                anchors.right: browserGlyph.right
                                anchors.top: browserGlyph.top
                                anchors.rightMargin: -Math.round(width * 0.25)
                                anchors.topMargin: Math.round(width * 0.15)

                                // Gentle attention pulse while lit — same
                                // alwaysRunToEnd discipline as the glyph pulse so
                                // it never freezes mid-fade; Theme.anim durations.
                                SequentialAnimation {
                                    running: attentionDot.visible
                                    loops: Animation.Infinite
                                    alwaysRunToEnd: true
                                    NumberAnimation {
                                        target: attentionDot; property: "opacity"
                                        to: 0.45; duration: Theme.anim.duration
                                        easing.type: Easing.InOutQuad
                                    }
                                    NumberAnimation {
                                        target: attentionDot; property: "opacity"
                                        to: 1.0; duration: Theme.anim.duration
                                        easing.type: Easing.InOutQuad
                                    }
                                }
                            }

                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: controller.focus_agent_browser(chip.slot)
                            }
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
