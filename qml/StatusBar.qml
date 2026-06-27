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

    // Live "now" (ms) for the rate-limit reset countdown — the Timer below ticks
    // it while the usage segment is visible. Seeded with Date.now() (not 0) so the
    // very first paint has a sane baseline; a 0 seed would render a ~19000-day
    // countdown for one frame before the Timer's triggeredOnStart corrects it.
    // Date.now() is fine in QML/JS (the ban is workflow-script-only).
    property double nowMs: Date.now()

    // Usage threshold colour — mirrors status-line.sh::get_usage_color.
    function _usageColor(pct) {
        if (pct >= 80) return Theme.color.usage.crit
        if (pct >= 50) return Theme.color.usage.warn
        return Theme.color.usage.good
    }

    // Compact countdown to a unix-epoch (seconds) reset; ports
    // status-line.sh::format_reset_countdown. "" when absent, "now" when elapsed.
    function _resetCountdown(resetEpochSec) {
        if (!resetEpochSec || resetEpochSec <= 0)
            return ""
        var diff = Math.floor(resetEpochSec - root.nowMs / 1000)
        if (diff <= 0)
            return "now"
        var d = Math.floor(diff / 86400)
        var h = Math.floor((diff % 86400) / 3600)
        var m = Math.floor((diff % 3600) / 60)
        if (d > 0) return d + "d" + h + "h"
        if (h > 0) return h + "h" + m + "m"
        return m + "m"
    }

    Timer {
        // Minute-precision countdown; 30s keeps the displayed "Xm" honest without
        // burning cycles. Only runs while the (Claude-gated) usage segment shows.
        interval: 30000
        repeat: true
        running: root.claudeAgentActive && controller.accountUsageValid
        triggeredOnStart: true   // seed nowMs the moment it starts
        onTriggered: root.nowMs = Date.now()
    }

    // One 5h/7d usage chip — "<label> <pct>% ⟲<countdown>". Inline component so
    // the two chips share one definition (DRY) instead of a copy-paste pair.
    component UsageChip: Row {
        id: chip
        property string label: ""
        property int pct: 0
        property int resetEpoch: 0
        spacing: Theme.spacing.xs

        Text {
            text: chip.label
            color: Theme.color.text.dim
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.sm
            renderType: Text.NativeRendering
        }
        Text {
            text: chip.pct + "%"
            color: root._usageColor(chip.pct)
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.sm
            renderType: Text.NativeRendering
        }
        Text {
            readonly property string cd: root._resetCountdown(chip.resetEpoch)
            visible: cd !== ""
            text: "⟲" + cd
            color: Theme.color.text.dim
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.xs
            renderType: Text.NativeRendering
        }
    }

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

                // Project name.
                Text {
                    visible: statusState.project !== ""
                    text: statusState.project
                    color: Theme.color.text.dim
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    Layout.alignment: Qt.AlignLeft | Qt.AlignVCenter
                    renderType: Text.NativeRendering
                }

                // Branch — prefixed with a git-like glyph.
                Row {
                    visible: statusState.branch !== ""
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
                        text: statusState.branch
                        color: Theme.color.text.normal
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
                spacing: Theme.spacing.md

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

                // context %
                Row {
                    spacing: Theme.spacing.xs
                    visible: controller.agentContextPct[controller.focusedAgent - 1] >= 0
                    Text {
                        text: "ctx"
                        color: Theme.color.text.dim
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        renderType: Text.NativeRendering
                    }
                    Text {
                        text: (controller.agentContextPct[controller.focusedAgent - 1] || 0) + "%"
                        color: root._usageColor(controller.agentContextPct[controller.focusedAgent - 1] || 0)
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        renderType: Text.NativeRendering
                    }
                }

                // account usage (5h / 7d) — universal data, shown only when valid
                Row {
                    spacing: Theme.spacing.md
                    visible: controller.accountUsageValid
                    UsageChip {
                        label: "5h"
                        pct: controller.accountUsage5hPct
                        resetEpoch: controller.accountUsage5hReset
                    }
                    UsageChip {
                        label: "7d"
                        pct: controller.accountUsage7dPct
                        resetEpoch: controller.accountUsage7dReset
                    }
                }
            }
        }

        // Sidebar-matched column. Width literal mirrors
        // `treeScope.Layout.minimumWidth/maximumWidth: 280` in
        // Main.qml — two call sites is the threshold for promoting
        // to a Theme token (per the §3 P2 note quoted in Main.qml
        // around gitPanelMaxFraction); a third use is when to
        // refactor. Stays empty: this column has no nvim-equivalent
        // status to surface; the chrome background continues the
        // bar visually under the tree without claiming the area
        // belongs to it.
        Item {
            Layout.preferredWidth: 280
            Layout.minimumWidth: 280
            Layout.maximumWidth: 280
            Layout.fillHeight: true
            visible: controller.treeVisible
        }
    }
}
