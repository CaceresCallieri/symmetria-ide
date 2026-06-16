// Embedded browser surface — the agentic-browser central pane.
//
// QtWebEngine WebEngineViews embedded as QML items, so the browser renders
// INTO this window (a sub-region of the IDE) and never creates its own
// top-level Hyprland surface. That embedding IS the containment: an
// agent-spawned external Chromium would appear as a separate window the
// compositor places (and the IDE can't track); a WebEngineView item cannot
// escape because it was never a window. See CLAUDE.md "The browser panes".
//
// STRUCTURE IS LOAD-BEARING (same rule as the agent surface): a FIXED
// `model: controller.maxBrowserSlots` Repeater over per-slot Loaders. Opening
// a window = the slot's `browserSlotActive[index]` flips true → the Loader
// instantiates a WebEngineView and loads its initial URL. Closing = the flag
// flips false → Loader teardown destroys the view (and its navigation
// history). Do NOT replace the fixed Repeater with a list-model over
// `browserOrder` — list churn would destroy/recreate sibling delegates and
// wipe live pages on every open/close.
//
// Navigation, title, and back/forward are all QML-local (WebEngineView owns
// them). Python only tracks bookkeeping (occupancy / display order / current
// title+url for the chip strip) and drives open/focus/close as state flips.
// All colour + typography binds against the `Theme` singleton.

import QtQuick
import QtQuick.Layouts
import QtWebEngine

import "design"

Item {
    id: root

    // Shared PERSISTENT profile so cookies / logins survive restarts (a
    // fresh, IDE-owned profile — importing the user's Chrome/Firefox cookies,
    // as cmux does, is a deliberate future option, not Stage 1).
    //
    // REGRESSION NOTE — do NOT inline `offTheRecord: false` as a declarative
    // property. Persistence needs offTheRecord=false WITH a storageName, but
    // the declarative form `storageName: "…"; offTheRecord: false` makes
    // QtWebEngine evaluate offTheRecord while storageName is still empty and
    // log "Storage name is empty. Cannot change profile from off-the-record
    // to disk-based behavior" on every launch. storageName-only is silent but
    // leaves offTheRecord=true (NOT persistent — logins lost on restart).
    // Setting offTheRecord AFTER storageName is applied (here, at completion)
    // is warning-free AND on-disk. Verified empirically on QtWebEngine 6.11.
    WebEngineProfile {
        id: browserProfile
        storageName: "symmetria-ide"
        Component.onCompleted: offTheRecord = false
    }

    // The focused slot's live WebEngineView, or null. On-demand (like the
    // agent surface's focusedTerminal) — NOT a reactive binding source, since
    // Loader.itemAt() isn't bindable. Reactive chrome (the address bar) reads
    // Python state instead; the nav buttons call through this helper.
    function focusedView() {
        var idx = controller.focusedBrowser - 1;
        if (idx < 0)
            return null;
        var loader = viewRepeater.itemAt(idx);
        return (loader && loader.item) ? loader.item : null;
    }

    // Address-bar input → a navigable URL. A bare host with a dot becomes
    // https://; anything with a space (or no dot) is treated as a search.
    function normalizeUrl(input) {
        var s = ("" + input).trim();
        if (s.length === 0)
            return "";
        if (s.indexOf("://") !== -1)
            return s;
        if (s.indexOf(" ") === -1 && s.indexOf(".") !== -1)
            return "https://" + s;
        return "https://www.google.com/search?q=" + encodeURIComponent(s);
    }

    // Re-grab page focus when the surface re-shows with an already-loaded
    // view (the per-view onVisibleChanged / focusBrowserRequested paths cover
    // the spawn + switch cases).
    onVisibleChanged: if (visible) {
        var v = focusedView();
        if (v)
            v.forceActiveFocus();
    }

    // Small clickable glyph button for the nav row (back / forward / reload).
    component NavButton: Item {
        id: navRoot
        property string glyph
        property bool enabledLook: true
        signal activated()

        width: Theme.size.modeBadgeHeight
        height: Theme.size.modeBadgeHeight

        Text {
            anchors.centerIn: parent
            text: navRoot.glyph
            color: navRoot.enabledLook ? Theme.color.text.normal : Theme.color.text.dim
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.md
            renderType: Text.NativeRendering
        }

        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: navRoot.activated()
        }
    }

    // ---- Chrome: tab chips (row 1) + nav controls & address bar (row 2) ----
    Rectangle {
        id: chromeBar
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: chromeColumn.implicitHeight + Theme.spacing.sm * 2
        color: Theme.color.bg.chrome

        // Hairline divider between the chrome and the viewport below — mirrors
        // the AgentTopBar / StatusBar 1px chrome edges.
        Rectangle {
            width: parent.width
            height: 1
            color: Theme.color.border.hairline
            anchors.bottom: parent.bottom
        }

        ColumnLayout {
            id: chromeColumn
            anchors.fill: parent
            anchors.leftMargin: Theme.spacing.md
            anchors.rightMargin: Theme.spacing.md
            anchors.topMargin: Theme.spacing.sm
            anchors.bottomMargin: Theme.spacing.sm
            spacing: Theme.spacing.sm

            // --- Row 1: open windows ("tabs"), dense display order + new ---
            Row {
                Layout.fillWidth: true
                spacing: Theme.spacing.sm

                Repeater {
                    // Display order (dense, compacts on close) — chip number is
                    // POSITION (index + 1); modelData is the internal slot used
                    // for focus/close, mirroring AgentTopBar exactly.
                    model: controller.browserOrder

                    delegate: Item {
                        id: tab

                        required property int index
                        required property int modelData

                        readonly property int slot: tab.modelData
                        readonly property int displayNumber: tab.index + 1
                        readonly property bool isFocused: controller.focusedBrowser === tab.slot
                        readonly property string pageTitle: controller.browserTitles[tab.slot - 1] || ""

                        height: Theme.size.modeBadgeHeight
                        implicitWidth: tabContent.implicitWidth + Theme.spacing.md * 2

                        PillSurface {
                            anchors.fill: parent
                            radius: height / 2
                            elevated: tab.isFocused
                            color: tab.isFocused ? Theme.color.bg.selected : "transparent"
                            borderColor: tab.isFocused ? Theme.color.border.hairline : "transparent"
                        }

                        Row {
                            id: tabContent
                            anchors.centerIn: parent
                            spacing: Theme.spacing.sm

                            Text {
                                anchors.verticalCenter: parent.verticalCenter
                                text: tab.displayNumber
                                color: tab.isFocused ? Theme.color.text.strong : Theme.color.text.dim
                                font.family: Theme.font.family
                                font.pixelSize: Theme.font.size.xs
                                font.weight: tab.isFocused ? Theme.font.weight.bold : Theme.font.weight.medium
                                font.letterSpacing: 0.6
                                renderType: Text.NativeRendering
                            }

                            Text {
                                anchors.verticalCenter: parent.verticalCenter
                                visible: tab.pageTitle !== ""
                                // U+2502 thin bar — matches the agent chip separator.
                                text: "│ " + tab.pageTitle
                                color: tab.isFocused ? Theme.color.text.strong : Theme.color.text.dim
                                font.family: Theme.font.family
                                font.pixelSize: Theme.font.size.xs
                                renderType: Text.NativeRendering
                                elide: Text.ElideRight
                                // Cap so a long page title can't push the strip
                                // past the bar; full title shows on the page.
                                width: Math.min(implicitWidth, 220)
                            }

                            Text {
                                anchors.verticalCenter: parent.verticalCenter
                                text: "✕"
                                color: Theme.color.text.dim
                                font.family: Theme.font.family
                                font.pixelSize: Theme.font.size.xs
                                renderType: Text.NativeRendering

                                MouseArea {
                                    anchors.fill: parent
                                    anchors.margins: -Theme.spacing.xs
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: controller.close_browser(tab.slot)
                                }
                            }
                        }

                        // Click anywhere else on the chip focuses the window.
                        // Declared AFTER the close ✕ MouseArea so the close
                        // target wins where they overlap (z-order = last wins).
                        MouseArea {
                            anchors.fill: parent
                            z: -1
                            cursorShape: Qt.PointingHandCursor
                            onClicked: controller.focus_browser(tab.slot)
                        }
                    }
                }

                // New-window chip.
                Item {
                    height: Theme.size.modeBadgeHeight
                    implicitWidth: Theme.size.modeBadgeHeight

                    PillSurface {
                        anchors.fill: parent
                        radius: height / 2
                        elevated: false
                        color: "transparent"
                        borderColor: Theme.color.border.hairline
                    }
                    Text {
                        anchors.centerIn: parent
                        text: "+"
                        color: Theme.color.text.normal
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.md
                        renderType: Text.NativeRendering
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: controller.open_browser()
                    }
                }
            }

            // --- Row 2: back / forward / reload + address bar ---
            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.spacing.sm

                NavButton {
                    glyph: "←"
                    onActivated: {
                        var v = root.focusedView();
                        if (v)
                            v.goBack();
                    }
                }
                NavButton {
                    glyph: "→"
                    onActivated: {
                        var v = root.focusedView();
                        if (v)
                            v.goForward();
                    }
                }
                NavButton {
                    glyph: "↻"
                    onActivated: {
                        var v = root.focusedView();
                        if (v)
                            v.reload();
                    }
                }

                // Address bar. Reactive DISPLAY comes from Python state
                // (browserUrls, refreshed via on_browser_url → browserTabsChanged)
                // so it tracks navigation without binding to the view (which
                // would fight user edits). While focused the field is free to
                // edit; Enter navigates; blur reverts to the live URL.
                Rectangle {
                    Layout.fillWidth: true
                    height: Theme.size.modeBadgeHeight
                    radius: Theme.radius.sm
                    color: Theme.color.bg.selected
                    border.width: 1
                    border.color: urlField.activeFocus ? Theme.color.accent.focus : Theme.color.border.hairline

                    TextInput {
                        id: urlField
                        anchors.fill: parent
                        anchors.leftMargin: Theme.spacing.sm
                        anchors.rightMargin: Theme.spacing.sm
                        verticalAlignment: TextInput.AlignVCenter
                        clip: true
                        color: Theme.color.text.normal
                        selectionColor: Theme.color.accent.primary
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        renderType: Text.NativeRendering
                        selectByMouse: true

                        property string displayUrl: controller.focusedBrowser > 0
                            ? (controller.browserUrls[controller.focusedBrowser - 1] || "")
                            : ""
                        // Only overwrite the field from page state when the user
                        // isn't mid-edit; revert any abandoned edit on blur.
                        onDisplayUrlChanged: if (!activeFocus) text = displayUrl
                        onActiveFocusChanged: if (!activeFocus) text = displayUrl
                        Component.onCompleted: text = displayUrl

                        onAccepted: {
                            var target = root.normalizeUrl(text);
                            var v = root.focusedView();
                            if (v && target.length > 0) {
                                v.url = target;
                                v.forceActiveFocus();
                            }
                        }
                    }
                }
            }
        }
    }

    // ---- Viewport: the pool of WebEngineViews + empty state ----
    Item {
        id: viewport
        anchors.top: chromeBar.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom

        // Empty-state affordance — the surface is reachable with no windows
        // open (the AgentTopBar switcher). Names the open affordance so the
        // path is discoverable without docs (mirrors the agent surface).
        Column {
            anchors.centerIn: parent
            spacing: Theme.spacing.sm
            visible: controller.focusedBrowser === 0

            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "no browser windows open"
                color: Theme.color.text.dim
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.sm
                renderType: Text.NativeRendering
            }
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "press +  ·  Ctrl+Shift+B toggles this surface"
                color: Theme.color.text.normal
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.sm
                renderType: Text.NativeRendering
            }
        }

        Repeater {
            id: viewRepeater
            model: controller.maxBrowserSlots

            delegate: Loader {
                id: slotLoader

                required property int index
                readonly property int slot: slotLoader.index + 1

                anchors.fill: parent
                active: controller.browserSlotActive[slotLoader.index]
                visible: controller.focusedBrowser === slotLoader.slot

                sourceComponent: WebEngineView {
                    id: view
                    profile: browserProfile

                    onTitleChanged: controller.on_browser_title(slotLoader.slot, view.title)
                    onUrlChanged: controller.on_browser_url(slotLoader.slot, view.url.toString())

                    Component.onCompleted: {
                        // One-shot initial-URL read (deliberately NOT a binding;
                        // a live binding would fight user navigation — see
                        // AppController.browserUrls). The WebEngineView owns its
                        // url thereafter.
                        var u = controller.browserUrls[slotLoader.index];
                        view.url = (u && ("" + u).length > 0) ? ("" + u) : "about:blank";
                        if (visible)
                            forceActiveFocus();
                    }
                    onVisibleChanged: if (visible)
                        forceActiveFocus();

                    // Focus pull for the already-visible case (chord/chip
                    // pressed while another widget held focus and this slot was
                    // already showing) — mirrors the agent delegate.
                    Connections {
                        target: controller
                        function onFocusBrowserRequested(s) {
                            if (s === slotLoader.slot && view.visible)
                                view.forceActiveFocus();
                        }
                    }
                }
            }
        }
    }
}
