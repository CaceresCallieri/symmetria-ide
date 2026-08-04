// Native status bar.
// Replaces NeoVim's lualine — `runtime/init.lua` sets `laststatus=0`
// and emits structured capsules (mode, project, branch, file, pos)
// which map to properties on `statusState`. QML bindings do the rest.
//
// All color and typography values bind against the `Theme` singleton
// (`qml/design/Theme.qml`) — local palette/size literals belong in
// Theme, not here. See Theme.qml for the provenance of chrome colors
// (Symmetria Shell mattePill) and mode colors (wine_theme colorscheme).

import QtQuick
import QtQuick.Layouts

import "design"

Rectangle {
    id: root
    color: Theme.color.bg.chrome

    // Hairline divider between editor and status bar.
    Rectangle {
        width: root.width
        height: 1
        color: Theme.color.border.hairline
        anchors.top: root.top
    }

    // Wine_theme mode → color mapping (sources in Theme.color.mode):
    //   NORMAL   → keyword             (#C28B12)
    //   INSERT   → string              (#62BA46)
    //   VISUAL*  → term_bright_magenta (#D86DE9)
    //   REPLACE  → error_red           (#D2602D)
    //   COMMAND  → accent_blue         (#6D94E9)
    //   TERMINAL → term_bright_cyan    (#5BDFD8)

    // True only when the nvim editor is the actually-visible central
    // surface — i.e. agent and FM are not overlaid AND centralSurface
    // is "editor". Mirrors the editor pane's own visibility binding
    // in Main.qml (`!agentVisible && !fmVisible && editorVisible`),
    // so the mode badge / file / cursor position appear only when the
    // user can see the buffer they belong to. In terminal / agent / FM
    // mode these capsules would lie about a buffer the user isn't
    // looking at — they were the source of the "NORMAL pill while
    // I'm typing in the shell" confusion that motivated this split.
    readonly property bool editorActive: !controller.agentVisible
                                         && !controller.fmVisible
                                         && controller.editorVisible

    // The subscription-usage readout, exposed so Main.qml can hover-track it
    // and anchor the detail popup above it. The popup itself is mounted at
    // WINDOW scope, not here: this bar is `Theme.size.statusBarHeight` (24px)
    // tall and would clip any panel parented into it.
    readonly property alias usageIndicator: usageIndicator

    // True only when a CLAUDE agent is the visible surface. The status-line
    // fields (model / effort / context%) and the account-usage segment gate on
    // this: their data is Claude-specific, so showing it in the editor, FM,
    // terminal, or an OpenCode agent would be misleading. `agentActivity[slot-1]
    // .agentType` carries the focused harness (defaults to "claude" pre-activity;
    // app.py::agentActivity).
    //
    // MUST be `agentSurfaceVisible` (centralSurface == "agent"), NOT `agentVisible`
    // — the latter is the parked SDK AgentPane overlay flag (env-gated, ~always
    // False), so binding to it hid the whole cluster permanently. (`editorActive`
    // above gets away with `!agentVisible` only because its decisive term is
    // `editorVisible`.)
    readonly property bool claudeAgentActive: controller.agentSurfaceVisible
        && controller.focusedAgent >= 1
        && (controller.agentActivity[controller.focusedAgent - 1] || {}).agentType === "claude"

    // The usage threshold table, the nowMs clock and the countdown formatter
    // all used to live here. The clock and formatter left with the 5h/7d chips;
    // the tiers moved to `UsageFormat` when the usage panel became their second
    // consumer — three copies of the same thresholds (this file, the panel, and
    // status-line.sh) is how one of them drifts. The CONTEXT segment below is
    // the one remaining caller and binds `UsageFormat.usageColor` directly.

    // The surface switcher (terminal / editor / agents) lived here
    // centered until 2026-06-11 — it now sits at the left edge of
    // AgentTopBar.qml, freeing the bottom bar's center for capsules.

    // Two-column outer layout mirrors the editor+sidebar RowLayout
    // sibling above (Main.qml mainContent + 1px separator + treeScope).
    // The chrome background is one continuous strip across both
    // columns — the boundary exists only to pin status content to
    // mainContent's right edge instead of the window's right edge.
    RowLayout {
        anchors.fill: parent
        spacing: 0

        Item {
            id: editorColumn
            Layout.fillWidth: true
            Layout.fillHeight: true

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: Theme.spacing.md
                anchors.rightMargin: Theme.spacing.md
                spacing: Theme.spacing.md

                // Mode badge — colored block like lualine's mode indicator.
                // Hidden outside editor mode: the underlying `statusState.mode`
                // capsule still updates (nvim keeps publishing mode changes
                // even when its pane isn't visible), but surfacing "NORMAL"
                // while the user is typing into the terminal would read as
                // "the IDE thinks I'm in nvim" — confusing precisely because
                // the user just switched away from it.
                //
                // `Layout.alignment` MUST include `Qt.AlignLeft` explicitly,
                // not just `Qt.AlignVCenter`. In QtQuick.Layouts, a bare
                // `Qt.AlignVCenter` leaves the horizontal alignment bits
                // unset, which the last visible non-fillWidth item in the
                // row interprets as "center horizontally inside the
                // expanded trailing cell". In terminal mode (where `file`
                // is hidden and no other item claims fillWidth), the
                // trailing cell grew to consume all leftover space and
                // dragged the branch to the row's visual center. Explicit
                // `Qt.AlignLeft | Qt.AlignVCenter` pins every item to the
                // leading edge of its cell, so even an expanded cell
                // renders its child flush-left.
                Rectangle {
                    visible: root.editorActive && statusState.mode !== ""
                    Layout.alignment: Qt.AlignLeft | Qt.AlignVCenter
                    Layout.preferredHeight: Theme.size.modeBadgeHeight
                    Layout.preferredWidth: modeLabel.implicitWidth + 16  // 8px horizontal inset per side
                    radius: height / 2
                    color: {
                        switch (statusState.mode) {
                            case "INSERT": return Theme.color.mode.insert
                            case "VISUAL":
                            case "V-LINE":
                            case "V-BLOCK": return Theme.color.mode.visual
                            case "REPLACE": return Theme.color.mode.replace
                            case "COMMAND": return Theme.color.mode.command
                            case "TERMINAL": return Theme.color.mode.terminal
                            default: return Theme.color.mode.normal   // NORMAL + any unrecognised mode (SELECT, S-LINE, etc.)
                        }
                    }
                    Text {
                        id: modeLabel
                        anchors.centerIn: parent
                        text: statusState.mode
                        color: Theme.color.mode.badgeLabel
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        font.weight: Theme.font.weight.bold
                        font.letterSpacing: 0.8
                        renderType: Text.NativeRendering
                    }
                }

                // Project name. In the VPS location the nvim capsule still
                // describes the LOCAL project (nvim never leaves this
                // machine), so the label switches to the paired
                // `<server>:<repo>` identity instead.
                Text {
                    id: projectText
                    readonly property string projectLabel:
                        controller.location === "vps"
                            ? controller.vpsProjectLabel
                            : statusState.project
                    visible: projectText.projectLabel !== ""
                    text: projectText.projectLabel
                    color: Theme.color.text.dim
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    Layout.alignment: Qt.AlignLeft | Qt.AlignVCenter
                    renderType: Text.NativeRendering
                }

                // Location badge — visible only in the VPS context so the
                // active location is legible from EVERY surface (the top
                // bar's toggle can be scrolled out of mind; this can't).
                // Shows the paired server's registry name, falling back to
                // a generic "vps" if the name is somehow empty.
                Rectangle {
                    visible: controller.location === "vps"
                    color: Theme.color.bg.selected
                    border.color: Theme.color.border.hairline
                    border.width: 1
                    radius: height / 2
                    implicitHeight: vpsBadgeLabel.implicitHeight + Theme.spacing.xs
                    implicitWidth: vpsBadgeLabel.implicitWidth + Theme.spacing.md
                    Layout.alignment: Qt.AlignLeft | Qt.AlignVCenter
                    Text {
                        id: vpsBadgeLabel
                        anchors.centerIn: parent
                        text: "⇅ " + (controller.vpsServerName !== "" ? controller.vpsServerName : "vps")
                        color: Theme.color.accent.primary
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        font.weight: Theme.font.weight.bold
                        font.letterSpacing: 0.6
                        renderType: Text.NativeRendering
                    }
                }

                // Branch — prefixed with a git-like glyph, followed by the
                // ahead/behind commit counts vs. the branch's upstream.
                // Branch NAME source is location-dependent: locally it's the
                // nvim capsule; in the VPS location it's GitController's
                // porcelain `# branch.head` (the remote repo's branch —
                // nvim knows nothing about it). Ahead/behind already read
                // gitController, which tracks the active location's repo.
                Row {
                    id: branchRow
                    readonly property string branchLabel:
                        controller.location === "vps"
                            ? gitController.branchName
                            : statusState.branch
                    visible: branchRow.branchLabel !== ""
                    spacing: Theme.spacing.xs
                    Layout.alignment: Qt.AlignLeft | Qt.AlignVCenter
                    Text {
                        text: "⎇"   // ⎇ branch glyph
                        color: Theme.color.accent.primary
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        renderType: Text.NativeRendering
                    }
                    Text {
                        text: branchRow.branchLabel
                        color: Theme.color.text.normal
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        renderType: Text.NativeRendering
                    }
                    // Unpushed (↑) / unpulled (↓) commit counts. Sourced from
                    // GitController's porcelain `# branch.ab` header — a
                    // DIFFERENT producer than the branch NAME above (the nvim
                    // `branch` capsule on statusState), but both describe the
                    // same repo in normal operation (nvim's cwd tracks
                    // displayedRoot, which drives gitController.repoRoot). Each
                    // shows only when non-zero, so a fully-synced branch renders
                    // just the name — matching the reference. `↑N` is the
                    // "commits you haven't pushed" count; `↓N` (meaningful only
                    // after a fetch) is its symmetric companion.
                    Text {
                        visible: gitController.aheadCount > 0
                        text: "↑" + gitController.aheadCount
                        color: Theme.color.accent.primary
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        renderType: Text.NativeRendering
                    }
                    Text {
                        visible: gitController.behindCount > 0
                        text: "↓" + gitController.behindCount
                        color: Theme.color.accent.primary
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        renderType: Text.NativeRendering
                    }
                }

                // The focused-agent info (model · effort · context% · 5h/7d
                // usage) is NOT inline here — it's consolidated into one CENTERED
                // group (`agentInfo` below, a sibling of this RowLayout) so all
                // agent data sits together in the middle of the bar while project
                // + branch stay flush-left.

                // File path (relative to cwd where possible).
                // Gated on `editorActive` so it disappears in terminal /
                // agent / FM mode — when nvim has no real buffer open
                // it publishes "[No Name]" via the `file` capsule, which
                // read as semantically empty noise while the user was
                // working in the shell. Workspace context (project,
                // branch) stays visible.
                //
                // `Layout.fillWidth: root.editorActive` keeps the slack
                // claim bound to the surface mode — in editor mode
                // the file expands to push position to the right edge;
                // in terminal mode the slack claim collapses so other
                // items don't get pushed around by a phantom fillWidth.
                Text {
                    visible: root.editorActive
                    text: statusState.file
                    color: Theme.color.text.strong
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    font.weight: Theme.font.weight.medium
                    Layout.alignment: Qt.AlignLeft | Qt.AlignVCenter
                    Layout.fillWidth: root.editorActive
                    elide: Text.ElideMiddle
                    renderType: Text.NativeRendering
                }

                // Trailing spacer — claims leftover row space ONLY in
                // non-editor modes, where `file` (the editor mode's
                // fillWidth item) is hidden and would otherwise leave
                // no item claiming slack. With no fillWidth item at
                // all, QtQuick.Layouts grows the last visible item's
                // cell to absorb leftover space, which is what dragged
                // the branch to the row's center in terminal mode.
                // This spacer guarantees there's always exactly one
                // fillWidth claimant — file in editor mode, this
                // spacer otherwise — so the visible status items keep
                // their cells the right size and stay packed flush-left.
                Item {
                    Layout.fillWidth: !root.editorActive
                    Layout.fillHeight: true
                }

                // Cursor position — pinned to the right edge of
                // `editorColumn` (which matches mainContent's right
                // edge above). Hidden outside editor mode for the
                // same reason as the mode badge: in terminal/agent
                // surfaces it would name a cursor the user can't
                // see, and the position capsule keeps publishing
                // CursorMoved events even when its pane is offscreen.
                Text {
                    visible: root.editorActive && statusState.position !== ""
                    text: statusState.position
                    color: Theme.color.text.dim
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    Layout.alignment: Qt.AlignRight | Qt.AlignVCenter
                    renderType: Text.NativeRendering
                }
            }

            // --- Focused-agent info, CENTERED ---
            // All agent data (model · effort · context% · 5h/7d usage) grouped
            // together and centered in the bar, while project + branch stay
            // flush-left in the RowLayout above. A SIBLING of that RowLayout
            // (not a row item) so it anchors to editorColumn's true horizontal
            // centre regardless of the left content's width; it overlays the
            // RowLayout's trailing spacer (empty on the agent surface), so there
            // is no collision. Visible only while a Claude agent is focused.
            Row {
                id: agentInfo
                visible: root.claudeAgentActive
                anchors.horizontalCenter: parent.horizontalCenter
                anchors.verticalCenter: parent.verticalCenter
                // Generous inter-MODULE gap (model:effort | ctx | Done at |
                // sessions) — the wide gap is the separator, replacing the
                // terminal line's " | ". Intra-module gaps stay tight (0 / xs /
                // sm) so each group reads as one unit.
                spacing: Theme.spacing.xl

                // model + effort, tight: ":effort" abuts the model with no gap,
                // matching the terminal status line's "Opus 4.8:xhigh".
                Row {
                    spacing: 0
                    visible: (controller.agentModels[controller.focusedAgent - 1] || "") !== ""
                    Text {
                        text: controller.agentModels[controller.focusedAgent - 1] || ""
                        color: Theme.color.text.normal
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        renderType: Text.NativeRendering
                    }
                    Text {
                        visible: (controller.agentEfforts[controller.focusedAgent - 1] || "") !== ""
                        text: ":" + (controller.agentEfforts[controller.focusedAgent - 1] || "")
                        color: Theme.color.text.dim
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        renderType: Text.NativeRendering
                    }
                }

                // context: "Ctx <used/limit> <pct>%" — the token counts are the
                // important part (mirrors the bash line's "Ctx: 34k/1000k (3%)").
                // agentContextDisplay carries the bash-formatted "used/limit";
                // the pct keeps the threshold colour.
                Row {
                    spacing: Theme.spacing.xs
                    visible: controller.agentContextPct[controller.focusedAgent - 1] >= 0
                    Text {
                        text: "Ctx"
                        color: Theme.color.text.dim
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        renderType: Text.NativeRendering
                    }
                    Text {
                        readonly property string disp: controller.agentContextDisplay[controller.focusedAgent - 1] || ""
                        visible: disp !== ""
                        text: disp
                        color: Theme.color.text.normal
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        renderType: Text.NativeRendering
                    }
                    Text {
                        text: (controller.agentContextPct[controller.focusedAgent - 1] || 0) + "%"
                        color: UsageFormat.usageColor(controller.agentContextPct[controller.focusedAgent - 1] || 0)
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        renderType: Text.NativeRendering
                    }
                }

                // last-finished time ("Done at HH:MM") — the agent's last Stop
                // hook, mirroring the bash status line's stop-timestamp.sh.
                // agentDoneAt is an epoch; Qt.formatTime renders 24h HH:mm. Hidden
                // until the agent has finished at least one turn.
                Row {
                    spacing: Theme.spacing.xs
                    visible: (controller.agentDoneAt[controller.focusedAgent - 1] || 0) > 0
                    Text {
                        text: "Done at"
                        color: Theme.color.text.dim
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        renderType: Text.NativeRendering
                    }
                    Text {
                        text: Qt.formatTime(new Date((controller.agentDoneAt[controller.focusedAgent - 1] || 0) * 1000), "HH:mm")
                        color: Theme.color.text.normal
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        renderType: Text.NativeRendering
                    }
                }

                // The account-usage 5h/7d chips used to sit here, gated on
                // `claudeAgentActive` — so they were visible only while looking
                // at a Claude agent, which is precisely when the user least
                // needs to be told. They now live in `UsageIndicator` in this
                // bar's right-hand column: always visible, multi-provider, and
                // kept fresh by a poller instead of by agent turns.
            }
        }

        // Sidebar-matched column. Width literal mirrors
        // `treeScope.Layout.minimumWidth/maximumWidth: 280` in
        // Main.qml — two call sites is the threshold for promoting
        // to a Theme token (per the §3 P2 note quoted in Main.qml
        // around gitPanelMaxFraction); a third use is when to
        // refactor. It carried no content at all until the usage
        // panel landed — there is no nvim-equivalent status to put
        // under the tree, but ACCOUNT-level state belongs to no
        // pane, which makes this the one honest home for it.
        Item {
            Layout.preferredWidth: 280
            Layout.minimumWidth: 280
            Layout.maximumWidth: 280
            Layout.fillHeight: true
            // Also shown with the tree hidden, so the readout does not
            // vanish with a panel it has nothing to do with.
            visible: controller.treeVisible || usageIndicator.hasRows

            UsageIndicator {
                id: usageIndicator
                anchors.right: parent.right
                anchors.rightMargin: Theme.spacing.md
                anchors.verticalCenter: parent.verticalCenter
                providers: controller.usageProviders
                refreshing: controller.usageRefreshing
                // The controller call lives here, not in the component: the
                // readout stays context-property-free (see its `refreshRequested`).
                onRefreshRequested: controller.refresh_usage()
            }
        }
    }
}
