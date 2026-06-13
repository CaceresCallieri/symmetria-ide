// Full-window agent surface — SDK sidecar event log + composer.
//
// Toggled on/off by `controller.agentVisible`. When visible, the
// pane replaces the editor entirely (see Main.qml's `mainContent`
// editor | terminal | AgentPane | FM swap). Not a side panel — the user's stated
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
//   - Escape OR Ctrl+X in the composer (or pane chrome) calls
//     `controller.hide_agent()` — Main.qml's `onVisibleChanged`
//     handler then returns focus to the editor, where
//     `<leader>aN` can spawn a fresh agent slot in the pool.
//     Both bindings exist because Escape is the cross-platform
//     reflex while Ctrl+X is the user's preferred binding for the
//     same intent — they coexist rather than compete.
//   - Ctrl+1..Ctrl+5 (composer or pane chrome) → focus_instance(N).
//     Mirrors the nvim-side `<C-1>..<C-5>` bindings installed in
//     `runtime/init.lua` — those only fire when the editor has
//     focus, so the pane needs its own QML-side handler when the
//     user is mid-conversation. The bright bubble in AgentTopBar
//     and the ListView's transcript both re-bind on the focus
//     switch.
//   - Ctrl+Shift+Q (composer or pane chrome) → close_focused_instance().
//     Mirrors the nvim-side `<C-S-q>` keymap. Closing the last
//     instance hides the pane and clears the AgentTopBar chip strip;
//     refocus picks a neighbour via `_next_focus_after_close`.
//   - Enter submits via `controller.submit_prompt(text)` and clears
//     the field. `controller.submit_prompt` routes to the Node
//     SDK sidecar; the event log accumulates across submissions so
//     the pane reads as a running history.
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

    // Cycle the SDK permission mode. Wired to Shift+Tab on both `root`
    // (when focus is on the events ListView or chrome) and `composer`
    // (when the user is typing). Centralised here so the two key
    // handlers don't drift out of sync.
    function _cyclePermissionMode() {
        controller.cycle_permission_mode()
    }

    // Map the four SDK permission modes to the Theme.color.permissionMode
    // rung. Default falls through for unknown / future modes so a
    // stray sidecar event can't paint the pill into a TypeError.
    function _pillColor(mode) {
        if (mode === "acceptEdits") return Theme.color.permissionMode.acceptEdits
        if (mode === "bypassPermissions") return Theme.color.permissionMode.bypassPermissions
        if (mode === "plan") return Theme.color.permissionMode.plan
        return Theme.color.permissionMode.default_
    }

    // Returns true for Shift+Tab regardless of how Qt represents it —
    // Qt.Key_Backtab is the canonical representation on most platforms,
    // but some input methods send Key_Tab + ShiftModifier instead. Both
    // are accepted so neither path silently misses the handler.
    function _isShiftTab(event) {
        return event.key === Qt.Key_Backtab
            || (event.key === Qt.Key_Tab && (event.modifiers & Qt.ShiftModifier))
    }

    // Returns true for Ctrl+X — the agent pane's "leave to editor"
    // affordance. Mirrors Escape's existing semantics (calls hide_agent),
    // but lives ALONGSIDE Escape rather than replacing it: Escape is the
    // common-platform reflex, Ctrl+X is the user's preferred binding for
    // the same intent. NB: inside a TextField, Ctrl+X is the system Cut
    // shortcut — handlers below MUST claim it via Keys.onShortcutOverride
    // first or Qt's accelerator routing intercepts and the composer just
    // clears the selected text instead of leaving the pane.
    function _isCtrlX(event) {
        return event.key === Qt.Key_X
            && (event.modifiers & Qt.ControlModifier)
    }

    // Returns the pool slot (1..5) when `event` is Ctrl+1..Ctrl+5, else 0.
    // Mirrors the nvim-side `<C-1>..<C-5>` focus keymaps installed in
    // `runtime/init.lua::install_agent_keymaps` — those only fire while
    // NeoVim has focus, so the agent pane needs its own QML handler for
    // when the user is typing in the composer or browsing the event log.
    // Calls `controller.focus_instance(slot)` which is a no-op when the
    // requested slot is empty (logs a warning), so pressing Ctrl+5 with
    // only 3 active slots is harmless.
    function _ctrlDigitSlot(event) {
        if (!(event.modifiers & Qt.ControlModifier)) return 0
        if (event.key < Qt.Key_1 || event.key > Qt.Key_5) return 0
        return event.key - Qt.Key_1 + 1
    }

    // Returns true for Ctrl+Shift+Q — the agent pane's "close focused
    // Claude Code instance" affordance. Mirrors the nvim-side `<C-S-q>`
    // keymap in `runtime/init.lua` so the close binding works regardless
    // of whether the editor or the composer holds focus. The empty-pool
    // case is handled by `_handle_agent_close` (hides pane, resets
    // focused instance to 1, emits all chrome signals so the AgentTopBar
    // chip strip clears).
    function _isCtrlShiftQ(event) {
        return event.key === Qt.Key_Q
            && (event.modifiers & Qt.ControlModifier)
            && (event.modifiers & Qt.ShiftModifier)
    }

    // Capture Shift+Tab when focus sits on the pane chrome (between
    // compositions, after escaping the composer, etc.). The composer's
    // own Keys.onPressed below carries the same handler so cycling
    // works while typing too. CRITICAL invariant: this handler ONLY
    // fires while the agent pane is `visible: true` — Main.qml's
    // editor/agent swap puts the pane in a `visible: false` Item when
    // the editor is active, so Shift+Tab in the editor (NeoVim's
    // outdent) cannot collide with this handler. Per non-negotiable #3
    // ("NeoVim motions preserved"), the editor's keybinds are sacred.
    Keys.onShortcutOverride: (event) => {
        if (root._isShiftTab(event)
                || root._isCtrlX(event)
                || root._isCtrlShiftQ(event)
                || root._ctrlDigitSlot(event) > 0)
            event.accepted = true
    }
    Keys.onPressed: (event) => {
        if (root._isShiftTab(event)) {
            root._cyclePermissionMode()
            event.accepted = true
        } else if (root._isCtrlX(event)) {
            controller.hide_agent()
            event.accepted = true
        } else if (root._isCtrlShiftQ(event)) {
            controller.close_focused_instance()
            event.accepted = true
        } else {
            const slot = root._ctrlDigitSlot(event)
            if (slot > 0) {
                controller.focus_instance(slot)
                event.accepted = true
            }
        }
    }

    ColumnLayout {
        anchors.fill: root
        anchors.leftMargin: root.horizontalPadding
        anchors.rightMargin: root.horizontalPadding
        anchors.topMargin: root.verticalPadding
        anchors.bottomMargin: root.verticalPadding
        spacing: Theme.spacing.md

        // --- Permission mode pill -----------------------------------
        //
        // Surfaces the sidecar's authoritative `permissionMode` so the
        // user always knows whether bypassPermissions is actually
        // active (versus assumed). Bound to controller.permissionMode
        // — the cycle slot does NOT optimistically mutate, so the pill
        // only flips after the SDK accepts the transition.
        //
        // Visual construction mimics StatusBar.qml's mode badge for
        // visual continuity with the editor pane: pill radius is
        // `height / 2`, label color is `Theme.color.mode.badgeLabel`,
        // background derives from the Theme.color.permissionMode rung
        // (which itself aliases mode.* hexes — see Theme.qml comment).
        RowLayout {
            id: chromeRow
            Layout.fillWidth: true
            Layout.preferredHeight: Theme.size.modeBadgeHeight
            spacing: Theme.spacing.md

            Rectangle {
                id: permissionPill
                Layout.preferredHeight: Theme.size.modeBadgeHeight
                Layout.preferredWidth: pillLabel.implicitWidth + Theme.spacing.md * 2
                radius: height / 2
                color: root._pillColor(controller.permissionMode)

                Text {
                    id: pillLabel
                    anchors.centerIn: parent
                    text: controller.permissionMode.toUpperCase()
                    color: Theme.color.mode.badgeLabel
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    font.weight: Theme.font.weight.medium
                    font.letterSpacing: 0.6
                    renderType: Text.NativeRendering
                }

                // Click-to-cycle as a mouse-friendly affordance (the
                // primary path is still Shift+Tab, per non-negotiable #1
                // — keyboard-first, mouse never required). The cursor
                // shape signals interactivity without a tooltip.
                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root._cyclePermissionMode()
                }
            }

            Text {
                id: pillHint
                Layout.fillWidth: true
                text: "Shift+Tab to cycle"
                color: Theme.color.text.dim
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.xs
                font.weight: Theme.font.weight.normal
                font.letterSpacing: 0.4
                renderType: Text.NativeRendering
            }

            // Multi-instance bubble strip lives in `qml/AgentTopBar.qml`
            // now (always-on top chrome). Keeping it out of this pane's
            // chromeRow means the user can see the pool topology even
            // when the editor is focused.
        }

        // --- Event log ----------------------------------------------
        ListView {
            id: events

            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true

            // Bind to `controller.sessionModelForFocused` so that
            // focus-switching keybinds (`<C-1>..<C-5>`) re-bind the
            // ListView to the new slot's transcript when focus changes.
            // A property with `notify=focusedInstanceChanged` is the
            // QML-native way to make this re-evaluate automatically.
            // The Connections block below is belt-and-suspenders:
            // PySide6's property-change detection for QObject-typed
            // return values (SessionModel* is a QObject subclass) can
            // miss an identity swap if the notify signal fires but the
            // engine skips the QObject* pointer comparison. The
            // imperative re-assignment guarantees the swap regardless.
            model: controller.sessionModelForFocused
            spacing: Theme.spacing.md

            Connections {
                target: controller
                function onFocusedInstanceChanged() {
                    events.model = controller.sessionModelForFocused
                }
            }

            // Reusable delegates keep long streams smooth (§3 P1:
            // Repeater would pre-instantiate every event).
            reuseItems: true
            cacheBuffer: 300

            // Always auto-stick to bottom while new events arrive.
            // Use `positionViewAtEnd` (not `positionViewAtIndex(count-1)`)
            // so the footer — the loading indicator — counts as part
            // of the "bottom" the view follows. Otherwise the spinner
            // would render off-screen below the last row whenever the
            // conversation already filled the viewport.
            onCountChanged: events.positionViewAtEnd()

            // positionViewAtEnd is NOT enough on its own: it fires from
            // onCountChanged when the user-message row is inserted, but at
            // that point QML's lazy binding evaluation hasn't yet updated the
            // footer's height (awaitingResponse just became true in the same
            // call frame). A second scroll-to-end fires here once the footer
            // height binding settles so the spinner is always fully visible.
            Connections {
                target: controller
                function onAwaitingResponseChanged() {
                    if (controller.awaitingResponse) events.positionViewAtEnd()
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
                // Populated only on permission_request rows. Drive the
                // approve/deny card below; empty strings everywhere
                // else collapse the card to height 0.
                required property string permissionState
                required property string requestId

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
                        color: _roleColor(entry.role, entry.kind)
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        font.weight: Theme.font.weight.medium
                        font.letterSpacing: 0.6
                        renderType: Text.NativeRendering
                    }

                    Text {
                        id: bodyText

                        // Diff rows render below in `diffView` instead —
                        // this Text would be a duplicate (and unwrapped)
                        // copy of the unified-diff string.
                        visible: entry.text !== "" && entry.kind !== "tool_diff"
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

                    // --- Diff variant ---------------------------------
                    //
                    // Only populated for `kind === "tool_diff"` rows.
                    // SessionModel emits these for Edit/Write/MultiEdit/
                    // NotebookEdit results, with `text` containing a
                    // `difflib.unified_diff` string. We split on \n and
                    // give each line its own tinted Rectangle so adds /
                    // removes / hunk markers read at a glance — the
                    // Claude Code terminal display the user pointed at
                    // for reference uses the same per-line tint pattern.
                    //
                    // No syntax highlighting yet (deferred Stage 3).
                    // Tokens live in `Theme.color.diff.*` with provenance
                    // comments — adding new diff colors lands there, not
                    // inline here.
                    Column {
                        id: diffView

                        readonly property bool isDiff: entry.kind === "tool_diff" && entry.text !== ""
                        readonly property list<string> lines: diffView.isDiff ? entry.text.split("\n") : []

                        visible: diffView.isDiff
                        width: entry.width
                        spacing: 0  // tight stacking — reads as a contiguous patch block

                        Repeater {
                            model: diffView.lines

                            delegate: Rectangle {
                                id: diffLine

                                required property string modelData

                                width: diffView.width
                                // Same vertical rhythm as the body text but tighter
                                // top/bottom padding so a 20-line diff doesn't double
                                // the row height.
                                implicitHeight: diffLineText.implicitHeight + Theme.spacing.xs
                                color: entry._diffBg(diffLine.modelData)

                                Text {
                                    id: diffLineText

                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.leftMargin: Theme.spacing.sm
                                    anchors.rightMargin: Theme.spacing.sm
                                    anchors.verticalCenter: parent.verticalCenter

                                    text: diffLine.modelData
                                    color: entry._diffFg(diffLine.modelData)
                                    font.family: Theme.font.family
                                    font.pixelSize: Theme.font.size.sm
                                    // Diffs are inherently column-positional —
                                    // wrapping breaks alignment between adds
                                    // and removes. Long lines elide instead.
                                    wrapMode: Text.NoWrap
                                    elide: Text.ElideRight
                                    textFormat: Text.PlainText
                                    renderType: Text.NativeRendering
                                }
                            }
                        }
                    }

                    // --- Permission card variant ----------------------
                    //
                    // Visible-gated container that renders the inline
                    // approve/deny affordance. Anchored under the body
                    // text so the card reads as part of the permission
                    // row's content, not as a sibling chrome element.
                    // Uses Rectangle+MouseArea instead of QtQuick.Controls
                    // Button so every pixel binds Theme tokens directly
                    // — no fight with the controls theme.
                    Item {
                        id: permissionCard

                        readonly property bool isPermission: entry.kind === "permission_request"
                        readonly property bool isPending: entry.permissionState === "pending"
                        readonly property bool isApproved: entry.permissionState === "approved"
                        readonly property bool isDenied: entry.permissionState === "denied"

                        visible: permissionCard.isPermission
                        width: entry.width
                        height: permissionCard.isPermission
                            ? permissionFrame.implicitHeight + Theme.spacing.sm
                            : 0

                        Rectangle {
                            id: permissionFrame
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.topMargin: Theme.spacing.sm
                            implicitHeight: permissionInner.implicitHeight + Theme.spacing.md * 2
                            color: Theme.color.bg.selected
                            radius: Theme.radius.sm
                            border.color: Theme.color.agent.permissionBorder
                            border.width: 1

                            Item {
                                id: permissionInner
                                anchors.fill: parent
                                anchors.margins: Theme.spacing.md
                                implicitHeight: pendingButtons.visible
                                    ? pendingButtons.implicitHeight
                                    : statusLabel.implicitHeight

                                // Pending state: two clickable
                                // affordances. Allow on the left, Deny
                                // on the right — reading order matches
                                // the safer-default pattern seen in
                                // editor permission prompts (Allow
                                // foregrounded, Deny secondary).
                                Row {
                                    id: pendingButtons
                                    anchors.left: parent.left
                                    anchors.verticalCenter: parent.verticalCenter
                                    spacing: Theme.spacing.md
                                    visible: permissionCard.isPending

                                    Rectangle {
                                        id: allowBtn
                                        implicitWidth: allowText.implicitWidth + Theme.spacing.lg * 2
                                        implicitHeight: allowText.implicitHeight + Theme.spacing.sm * 2
                                        color: Theme.color.agent.permissionApprove
                                        radius: Theme.radius.sm

                                        Text {
                                            id: allowText
                                            anchors.centerIn: parent
                                            text: "Allow"
                                            color: Theme.color.mode.badgeLabel
                                            font.family: Theme.font.family
                                            font.pixelSize: Theme.font.size.xs
                                            font.weight: Theme.font.weight.medium
                                            font.letterSpacing: 0.6
                                            renderType: Text.NativeRendering
                                        }

                                        MouseArea {
                                            anchors.fill: parent
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: controller.respond_to_permission(
                                                entry.requestId, "allow")
                                        }
                                    }

                                    Rectangle {
                                        id: denyBtn
                                        implicitWidth: denyText.implicitWidth + Theme.spacing.lg * 2
                                        implicitHeight: denyText.implicitHeight + Theme.spacing.sm * 2
                                        color: Theme.color.agent.permissionDeny
                                        radius: Theme.radius.sm

                                        Text {
                                            id: denyText
                                            anchors.centerIn: parent
                                            text: "Deny"
                                            color: Theme.color.mode.badgeLabel
                                            font.family: Theme.font.family
                                            font.pixelSize: Theme.font.size.xs
                                            font.weight: Theme.font.weight.medium
                                            font.letterSpacing: 0.6
                                            renderType: Text.NativeRendering
                                        }

                                        MouseArea {
                                            anchors.fill: parent
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: controller.respond_to_permission(
                                                entry.requestId, "deny")
                                        }
                                    }
                                }

                                // Resolved state: status label.
                                Text {
                                    id: statusLabel
                                    anchors.left: parent.left
                                    anchors.verticalCenter: parent.verticalCenter
                                    visible: !permissionCard.isPending
                                    text: permissionCard.isApproved
                                        ? "✓ approved"
                                        : permissionCard.isDenied
                                            ? "✗ denied"
                                            : ""
                                    color: permissionCard.isApproved
                                        ? Theme.color.agent.permissionApprove
                                        : Theme.color.agent.permissionDeny
                                    font.family: Theme.font.family
                                    font.pixelSize: Theme.font.size.sm
                                    font.weight: Theme.font.weight.medium
                                    renderType: Text.NativeRendering
                                }
                            }
                        }
                    }
                }

                function _roleColor(r, k) {
                    if (k === "permission_request") return Theme.color.agent.permissionBorder
                    if (r === "user") return Theme.color.agent.user
                    if (r === "assistant") return Theme.color.agent.assistant
                    // Tool-result rows borrow text.dim for their role
                    // label — they're machine output (reference material),
                    // intentionally quieter than user / assistant turns.
                    // Same rationale Theme.qml gives for system rows.
                    return Theme.color.text.dim
                }

                function _bodyColor(r) {
                    if (r === "user") return Theme.color.text.strong
                    if (r === "assistant") return Theme.color.text.emphasis
                    // tool / system / unknown all borrow text.dim — see
                    // _roleColor comment.
                    return Theme.color.text.dim
                }

                function _formatRoleLabel(r, s, k) {
                    if (k === "permission_request") return "Permission"
                    // Diff rows carry the tool name in subtype (e.g.
                    // "Edit", "Write") — show that directly rather than
                    // the generic "Tool result" label so the user sees
                    // *which* operation produced the patch.
                    if (k === "tool_diff") return s !== "" ? s : "Edit"
                    if (r === "user") return "You"
                    if (r === "assistant") return "Claude"
                    if (r === "tool") return s === "error" ? "Tool · error" : "Tool result"
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

                // --- Diff line tinting ----------------------------------
                //
                // Both helpers gate on `+++` / `---` prefixes (the unified-
                // diff file headers difflib emits) BEFORE the single-char
                // `+` / `-` checks — the headers visually land at the top of
                // the patch and shouldn't be tinted as add/remove lines.
                // They borrow `text.dim` foreground so they read as
                // metadata rather than content.

                function _diffBg(line) {
                    if (line.length === 0) return "transparent"
                    if (line.indexOf("+++") === 0) return "transparent"
                    if (line.indexOf("---") === 0) return "transparent"
                    if (line.charAt(0) === "+") return Theme.color.diff.addedBg
                    if (line.charAt(0) === "-") return Theme.color.diff.removedBg
                    if (line.indexOf("@@") === 0) return Theme.color.diff.hunkBg
                    return "transparent"
                }

                function _diffFg(line) {
                    if (line.length === 0) return Theme.color.diff.contextFg
                    if (line.indexOf("+++") === 0) return Theme.color.text.dim
                    if (line.indexOf("---") === 0) return Theme.color.text.dim
                    if (line.charAt(0) === "+") return Theme.color.diff.addedFg
                    if (line.charAt(0) === "-") return Theme.color.diff.removedFg
                    if (line.indexOf("@@") === 0) return Theme.color.diff.hunkFg
                    return Theme.color.diff.contextFg
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

            // Loading indicator anchored to the end of the list. Sits
            // directly under the last response row so it reads as
            // "Claude is still working on this turn" rather than as
            // a generic chrome element. ListView manages footer
            // lifecycle and includes it when computing `contentHeight`,
            // so `positionViewAtEnd` above scrolls past it correctly.
            //
            // Aesthetic is intentionally minimal — the boolean is the
            // infrastructure; a richer indicator (animated dots,
            // branded glyph) is a follow-up once turn grouping lands.
            // Reserve a stable height (`visible ? ... : 0`) so the
            // viewport doesn't jump as the row toggles.
            footer: Item {
                // Single binding point for the spinner state — both
                // `height` and `visible` reference this so the same
                // expression isn't duplicated and the intent is obvious.
                readonly property bool isLoading: controller.awaitingResponse

                width: events.width
                // visible-gated height keeps the footer from claiming
                // a row of empty space between turns. `+ Theme.spacing.sm`
                // gives the spinner a small breath off the last row.
                height: isLoading
                    ? loaderText.implicitHeight + Theme.spacing.sm * 2
                    : 0
                visible: isLoading

                Text {
                    id: loaderText

                    anchors.left: parent.left
                    anchors.top: parent.top
                    anchors.topMargin: Theme.spacing.sm
                    text: "Claude is thinking…"
                    color: Theme.color.agent.assistant
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    font.weight: Theme.font.weight.medium
                    font.letterSpacing: 0.6
                    renderType: Text.NativeRendering

                    // Slow opacity pulse — cheap, no extra QtQuick
                    // controls, no rotation animation. Auto-pauses
                    // when the footer's `visible` is false (Qt
                    // freezes property animations on hidden ancestors)
                    // so it costs nothing the rest of the time.
                    SequentialAnimation on opacity {
                        running: controller.awaitingResponse
                        loops: Animation.Infinite
                        NumberAnimation { from: 1.0; to: 0.4; duration: 700 }
                        NumberAnimation { from: 0.4; to: 1.0; duration: 700 }
                    }
                }
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

                // Escape returns to the editor. Main.qml watches the
                // editor's `onVisibleChanged` to restore focus on the
                // far side, so no additional focus handling needed here.
                Keys.onEscapePressed: controller.hide_agent()

                // Shift+Tab while typing cycles permissionMode without
                // surrendering composer focus. TextField's default Tab
                // / Shift+Tab is focus navigation — we deliberately
                // override here because the only other focusable item in
                // the pane chrome is the pill's MouseArea, and the user
                // doesn't want focus to leave the composer mid-thought.
                // Shortcut+Override pair keeps Qt's accelerator system
                // from intercepting first.
                Keys.onShortcutOverride: (event) => {
                    if (root._isShiftTab(event)
                            || root._isCtrlX(event)
                            || root._isCtrlShiftQ(event)
                            || root._ctrlDigitSlot(event) > 0)
                        event.accepted = true
                }
                Keys.onPressed: (event) => {
                    if (root._isShiftTab(event)) {
                        root._cyclePermissionMode()
                        event.accepted = true
                    } else if (root._isCtrlX(event)) {
                        controller.hide_agent()
                        event.accepted = true
                    } else if (root._isCtrlShiftQ(event)) {
                        controller.close_focused_instance()
                        event.accepted = true
                    } else {
                        const slot = root._ctrlDigitSlot(event)
                        if (slot > 0) {
                            controller.focus_instance(slot)
                            event.accepted = true
                        }
                    }
                }
            }
        }
    }
}
