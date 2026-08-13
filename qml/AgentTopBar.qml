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
    // both location cues live on the right rail). Shares the flat
    // SegmentedControl with the surface switcher; Ctrl+Shift+U is the chord
    // twin. Same accepted-overlap caveat as the switcher's comment below,
    // mirrored to this edge: the centered chip strip could reach the toggle
    // only with many long-titled chips on a narrow window — verify visually
    // if that combination ever ships.
    SegmentedControl {
        id: locationToggle
        anchors.right: root.right
        anchors.rightMargin: Theme.spacing.md
        anchors.verticalCenter: root.verticalCenter
        z: 1
        visible: controller.vpsAvailable || controller.location === "vps"

        // Icon + active-only label, matching the surface switcher (user
        // decision 2026-08-13, reversing this control's first cut). The
        // earlier argument for keeping both words was that the saving is
        // small (two short words) while the cost of misreading which
        // location your commands run in is not -- a wrong surface is
        // obvious immediately, a wrong location only after you run
        // something. The counter-argument that won: the ACTIVE half is
        // still spelled out in words, so the state you are in is never the
        // one you have to infer from a glyph; only the state you are not in
        // is, and that one is named the instant you move to it.
        //
        // It stays SEGMENTED rather than collapsing to a cycling label
        // because the axis is expected to grow past two: with several
        // registered machines the right control is a dropdown, and a
        // segmented row degrades into one far more naturally than a
        // click-to-cycle label, which stops working entirely at N > 2.
        segments: [
            { key: "local", label: "Local", icon: Theme.glyph.location.local },
            { key: "vps", label: "VPS", icon: Theme.glyph.location.vps },
        ]
        current: controller.location
        onActivated: key => controller.set_location(key)
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
    SegmentedControl {
        id: surfaceSwitcher
        anchors.left: root.left
        anchors.leftMargin: Theme.spacing.md
        anchors.verticalCenter: root.verticalCenter
        z: 1

        // `key` is the controller's centralSurface value (the wire name stays
        // singular "agent"); `label` and `icon` are display-only.
        //
        // ICON + ACTIVE-ONLY LABEL (2026-08-13). This was four permanently
        // drawn words, the largest single block of chrome text in the IDE.
        // Now each surface keeps a mark and only the current one is named,
        // which is where every editor with a comparable control landed
        // independently (Zed, VS Code, JetBrains all show icons, none show a
        // row of words) - so the switcher reads as navigation rather than as
        // a settings row. A dropdown was considered and rejected here: this
        // is the most-used control in the window, and a dropdown spends a
        // click and a popup on every single use of it.
        segments: [
            { key: "terminal", label: "Terminal", icon: Theme.glyph.surface.terminal },
            { key: "editor", label: "Editor", icon: Theme.glyph.surface.editor },
            { key: "agent", label: "Agents", icon: Theme.glyph.surface.agent },
            // "git" not "history": the surface is dual-mode (a Tab-toggled
            // "changes" working-tree view + the commit log), and it opens on
            // changes — labelling it "history" would mis-name where the chip
            // lands.
            { key: "git", label: "Git", icon: Theme.glyph.surface.git },
            // NB: no "browser" segment, though there COULD be one now — the
            // browser became a real central surface again when Chrome moved
            // into the IDE's nested compositor. It is left out because the
            // browser is agent-owned: you reach it through the agent that
            // opened it (the globe on that agent's chip, below) or with
            // Ctrl+Shift+B. Adding a segment here is a product call, not an
            // impossibility — which is what this note used to claim.
        ]
        current: controller.centralSurface
        onActivated: key => controller.set_central_surface(key)
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

                        // Browser-link indicator — how you learn that an agent has
                        // a browser and that it wants your eyes on it. Shown when
                        // this agent owns ≥1 OPEN browser window (it opened one
                        // via browser_open). Clicking GOES to the browser
                        // surface — one-way, unlike the Ctrl+Shift+B toggle,
                        // since "show me that agent's browser" has no return
                        // direction. (It only REPORTED via a toast while Chrome
                        // was an external Hyprland window and going there meant
                        // a workspace switch the user never asked for; the
                        // nested compositor removed that cost.)
                        // The mouse twin of Ctrl+Shift+B. Two layers ride it:
                        //   - the GLOBE = presence ("this agent has a browser");
                        //   - the DOT   = attention ("…and it wants you to look"),
                        //     lit by the agent's browser_request_attention call,
                        //     cleared when you check in (focus_agent_browser,
                        //     which keeps its old name because it IS this binding).
                        // A third layer once existed — an `active` pulse bound to
                        // `agentBrowserActive` — removed with the embedded engine:
                        // page driving flows agent→chrome-devtools-mcp→CDP and
                        // never reaches us, so it could never fire again.
                        // Rendered in editorFontFamily (a Nerd Font) because the
                        // chip's UI font may lack icon glyphs. `agentBrowserCount`/
                        // `Attention` are PySide QVariantLists — index then guard,
                        // per qml_qvariantlist_array_check (no Array.isArray).
                        Item {
                            id: browserIndicator
                            anchors.verticalCenter: parent.verticalCenter
                            readonly property bool owns:
                                (controller.agentBrowserCount[chip.slot - 1] || 0) > 0
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
                                color: browserIndicator.attention
                                    ? Theme.color.text.strong
                                    : Theme.color.text.dim
                                renderType: Text.NativeRendering
                            }

                            // Attention dot — the notification badge the agent
                            // raises via browser_request_attention. Accent fill
                            // with a chrome-bg ring so it reads as a badge ATOP
                            // the globe's top-right (the conventional notify
                            // corner). The 0.34 size + corner offsets are one-off
                            // pixel ratios (local, per the header convention).
                            // Drawn after the glyph so it paints on top; the
                            // shared MouseArea below still wins the click, so
                            // tapping the badge reports + clears it.
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

                        // Worktree indicator — this agent's live root is a
                        // linked git worktree of the project (worktree
                        // follow); focusing the chip re-roots the tree + git
                        // chrome there. Presence-only (no dot/pulse layers —
                        // sibling browserIndicator carries those precedents
                        // if attention semantics ever grow here). Same
                        // QVariantList indexing discipline as above: index
                        // then coerce (no Array.isArray).
                        Item {
                            id: worktreeIndicator
                            anchors.verticalCenter: parent.verticalCenter
                            readonly property string worktreeName:
                                String(controller.agentWorktree[chip.slot - 1] || "")
                            visible: worktreeIndicator.worktreeName.length > 0
                            width: worktreeIndicator.visible
                                ? worktreeGlyph.implicitWidth : 0
                            height: worktreeGlyph.implicitHeight

                            Text {
                                id: worktreeGlyph
                                anchors.centerIn: parent
                                // nf-oct-git_branch — worktree mark. Escape
                                // form on purpose: a literal private-use-area
                                text: Theme.glyph.worktree
                                font.family: editorFontFamily
                                font.pixelSize: Theme.font.size.sm
                                // Accent (not text.dim): the glyph is a state
                                // badge — "this agent lives in a worktree" —
                                // and must read at a glance like the header's
                                // twin, not blend into the chip title.
                                color: Theme.color.accent.primary
                                renderType: Text.NativeRendering
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
