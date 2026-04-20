// Native cmdline + wildmenu overlay.
//
// Floats above NvimView (parented to it in Main.qml) at ~20% from the
// top, horizontally centered, width ~60% of viewport. Visibility is
// driven entirely by cmdlineState.visible and popupmenuModel.visible —
// no mouse/focus interaction; keys always flow to the underlying
// NvimView and NeoVim owns the cmdline state.
//
// The autocomplete list uses ListView.isCurrentItem for selected-row
// highlight (bound via `currentIndex: completionModel.selected`). Only
// the two affected delegates re-paint on selection change.

import QtQuick

Item {
    id: root
    anchors.fill: parent
    // Overlay shows whenever the cmdline is open. The popup slot tracks
    // `completionModel` (our own getcompletion-driven pipeline) rather
    // than the ext_popupmenu model so completions appear here regardless
    // of user plugin config (nvim-cmp, wilder, noice, etc.).
    visible: cmdlineState.visible
    z: 100

    // Palette — matches Symmetria Shell matte-pill chrome (derived from
    // `~/.config/quickshell/symmetria/services/Colours.qml` mattePill()
    // at intensity 0.3 = Colours.glass.subtle, the canonical value used
    // by the shell's bar pills). The cmdline and popup are overlays *on
    // top of* the transparent editor; using the same opaque matte as
    // the StatusBar keeps UI chrome visually coherent and readable
    // regardless of the wallpaper behind. Keep in sync with
    // StatusBar.qml's `color` and WhichKeyOverlay.qml's `color`.
    property color bgColor: "#201F1F"
    property color borderColor: "#1fffffff"       // white @ 12% alpha
    property color firstCharColor: "#c8a37a"
    property color textColor: "#e8e8e8"
    property color cursorColor: "#e8e8e8"
    property color popupBgColor: "#201F1F"  // intentionally equals bgColor — popup uses the same matte base as the cmdline strip
    property color popupBorderColor: "#1fffffff"  // white @ 12% alpha
    // Selected popup row — matte at intensity 0.7 ("strong") on the
    // same m3surfaceContainerHigh base, so the lift over `popupBgColor`
    // (subtle=0.3) stays proportionate to the shell's pill hierarchy.
    property color popupSelBgColor: "#282728"
    property color popupSelFgColor: "#f5f5f5"
    property color popupFgColor: "#b0b0b0"
    // Font family — bound to the context property `editorFontFamily`
    // exposed from Python (`AppController` → `app.py`), which runs the
    // actual `QFontDatabase` check and picks the same primary family
    // the grid (`NvimView._default_font`) chose. Keeping one resolver
    // in Python prevents drift between grid and overlay glyph choice.
    //
    // Pitfall: QML's `font.family` is a single QString — a comma-
    // separated string is NOT parsed as a fallback list (would be
    // treated as one literal family name). `font.families` (plural)
    // does NOT exist on QML's font value type in Qt 6.11 despite
    // being on `QFont` at the C++ level. So per-glyph cascade needs
    // a different mechanism (theme provider binding whole QFont) —
    // filed as a follow-up; for now the single resolved family gives
    // us correct nerd-font rendering on this machine.
    property string monoFont: editorFontFamily
    property int fontSize: 14
    property int rowHeight: 24
    property int maxPopupRows: 10

    // Cmdline + popup stacked together so the popup drops directly
    // beneath the input line.
    Column {
        id: stack
        width: Math.min(900, Math.max(420, root.width * 0.6))
        anchors.horizontalCenter: root.horizontalCenter
        y: Math.round(root.height * 0.20)
        spacing: 6

        Rectangle {
            id: cmdBox
            visible: cmdlineState.visible
            width: stack.width
            height: Math.max(42, cmdRow.implicitHeight + 18)
            radius: 6
            color: root.bgColor
            border.color: root.borderColor
            border.width: 1

            Row {
                id: cmdRow
                anchors.fill: cmdBox
                anchors.leftMargin: 14
                anchors.rightMargin: 14
                anchors.topMargin: 9
                anchors.bottomMargin: 9
                // Spacing is 0 so the cursor bar sits FLUSH against the
                // last pre-cursor glyph. Gaps where we actually want them
                // (prompt→firstchar, firstchar→text) are added via per-
                // item rightPadding below. A non-zero Row.spacing applies
                // uniformly between every child and visibly offsets the
                // caret from the text it's supposed to anchor to.
                spacing: 0

                // `input("Name: ")` prompt shows before firstchar.
                Text {
                    visible: cmdlineState.prompt !== ""
                    text: cmdlineState.prompt
                    color: root.textColor
                    font.family: root.monoFont
                    font.pixelSize: root.fontSize
                    anchors.verticalCenter: cmdRow.verticalCenter
                    rightPadding: 4
                    renderType: Text.NativeRendering
                }

                // Firstchar glyph — `:`, `/`, `?`, `=`. Accent color so
                // the mode of the cmdline reads at a glance.
                Text {
                    visible: cmdlineState.firstchar !== ""
                    text: cmdlineState.firstchar
                    color: root.firstCharColor
                    font.family: root.monoFont
                    font.pixelSize: root.fontSize
                    font.weight: Font.Bold
                    anchors.verticalCenter: cmdRow.verticalCenter
                    rightPadding: 4
                    renderType: Text.NativeRendering
                }

                // Text before cursor.
                Text {
                    id: textBefore
                    text: cmdlineState.text.substring(0, cmdlineState.cursorPos)
                    color: root.textColor
                    font.family: root.monoFont
                    font.pixelSize: root.fontSize
                    anchors.verticalCenter: cmdRow.verticalCenter
                    renderType: Text.NativeRendering
                }

                // 2px bar cursor. Simpler than block-over-char and avoids
                // having to measure the single character at cursorPos.
                Rectangle {
                    width: 2
                    height: root.fontSize + 4
                    color: root.cursorColor
                    anchors.verticalCenter: textBefore.verticalCenter
                }

                // Text after cursor.
                Text {
                    text: cmdlineState.text.substring(cmdlineState.cursorPos)
                    color: root.textColor
                    font.family: root.monoFont
                    font.pixelSize: root.fontSize
                    anchors.verticalCenter: cmdRow.verticalCenter
                    renderType: Text.NativeRendering
                }
            }
        }

        Rectangle {
            id: popupBox
            // popupList.count is reactive (unlike calling a model method
            // in a binding, which is the gotcha documented in CLAUDE.md).
            // Scoped to `cmdlineState.visible` so the popup disappears
            // when the cmdline closes even if the model still has items.
            visible: cmdlineState.visible && completionModel.visible && popupList.count > 0
            width: stack.width
            height: Math.min(root.maxPopupRows, popupList.count) * root.rowHeight + 12
            radius: 6
            color: root.popupBgColor
            border.color: root.popupBorderColor
            border.width: 1
            clip: true

            ListView {
                id: popupList
                anchors.fill: parent
                anchors.margins: 6
                model: completionModel
                // Selection comes from the Lua runtime via wildmenumode()
                // detection: during Tab cycling the list stays stable and
                // `completionModel.selected` points at the cycled row so
                // the delegate can highlight it. Outside of cycling the
                // value is -1 and nothing is highlighted.
                currentIndex: completionModel.selected
                interactive: false

                delegate: Rectangle {
                    id: delegateRow
                    width: popupList.width
                    height: root.rowHeight
                    color: ListView.isCurrentItem ? root.popupSelBgColor : "transparent"
                    radius: 3

                    Text {
                        anchors.fill: delegateRow
                        anchors.leftMargin: 10
                        anchors.rightMargin: 10
                        verticalAlignment: Text.AlignVCenter
                        text: model.word
                        color: ListView.isCurrentItem ? root.popupSelFgColor : root.popupFgColor
                        font.family: root.monoFont
                        font.pixelSize: root.fontSize - 1
                        elide: Text.ElideRight
                        renderType: Text.NativeRendering
                    }
                }
            }
        }
    }
}
