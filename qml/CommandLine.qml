// Native cmdline + wildmenu overlay.
//
// Floats above the editor terminal (parented to it in Main.qml) at ~20%
// from the top, horizontally centered, width ~60% of viewport. Visibility
// is driven entirely by cmdlineState.visible and completionModel.visible —
// no mouse/focus interaction; keys always flow to the underlying editor
// terminal and NeoVim owns the cmdline state.
//
// The autocomplete list uses ListView.isCurrentItem for selected-row
// highlight (bound via `currentIndex: completionModel.selected`). Only
// the two affected delegates re-paint on selection change.
//
// All color and typography values bind against the `Theme` singleton
// (`qml/design/Theme.qml`). Local literals belong in Theme, not here.

import QtQuick

import "design"

Item {
    id: root
    anchors.fill: parent
    // Overlay shows whenever the cmdline is open. The popup slot tracks
    // `completionModel` (our own getcompletion-driven pipeline) rather
    // than the ext_popupmenu model so completions appear here regardless
    // of user plugin config (nvim-cmp, wilder, noice, etc.).
    visible: cmdlineState.visible
    z: 100

    // Row height for a single popup entry. Local because only this
    // file cares about the relationship between row height and font
    // size, but the visible dimension comes from Theme.
    property int rowHeight: Theme.size.popupRowHeight
    property int maxPopupRows: 10
    // Uniform inset around the popup list (top+bottom+left+right). The height formula
    // uses 2 * popupInset so this is the single value to change when tuning density.
    property int popupInset: 5
    // Vertical padding inside the cmdline input box = top + bottom anchors (Theme.spacing.sm each)
    // + 2px breathing room for NativeRendering line-height expansion.
    // Must stay in sync with cmdRow's anchors.topMargin + anchors.bottomMargin.
    property int cmdBoxPaddingVertical: 2 * Theme.spacing.sm + 2  // = 14 at spacing.sm=6
    // Minimum cmdBox height — generous floor so the box never collapses below
    // a comfortable click target even for single-line content.
    property int cmdBoxMinHeight: Theme.font.size.md + cmdBoxPaddingVertical + 9  // = 34 at font.md=11

    // Cmdline + popup stacked together so the popup drops directly
    // beneath the input line.
    Column {
        id: stack
        width: Math.min(720, Math.max(360, root.width * 0.58))
        anchors.horizontalCenter: root.horizontalCenter
        y: Math.round(root.height * 0.20)
        spacing: Theme.spacing.sm

        Rectangle {
            id: cmdBox
            visible: cmdlineState.visible
            width: stack.width
            height: Math.max(root.cmdBoxMinHeight, cmdRow.implicitHeight + root.cmdBoxPaddingVertical)
            radius: Theme.radius.md
            // `bg.raised`: the cmdline box floats over the status bar, which
            // paints `bg.chrome`. Before the flat move a drop shadow separated
            // them; now the fill step is the only cue.
            color: Theme.color.bg.raised
            border.color: Theme.color.border.hairline
            border.width: 1

            Row {
                id: cmdRow
                anchors.fill: cmdBox
                anchors.leftMargin: Theme.spacing.md
                anchors.rightMargin: Theme.spacing.md
                anchors.topMargin: Theme.spacing.sm
                anchors.bottomMargin: Theme.spacing.sm
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
                    color: Theme.color.text.emphasis
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.md
                    anchors.verticalCenter: cmdRow.verticalCenter
                    rightPadding: Theme.spacing.xs
                    renderType: Text.NativeRendering
                }

                // Firstchar glyph — `:`, `/`, `?`, `=`. Accent color so
                // the mode of the cmdline reads at a glance.
                Text {
                    visible: cmdlineState.firstchar !== ""
                    text: cmdlineState.firstchar
                    color: Theme.color.accent.primary
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.md
                    font.weight: Theme.font.weight.bold
                    anchors.verticalCenter: cmdRow.verticalCenter
                    rightPadding: Theme.spacing.xs
                    renderType: Text.NativeRendering
                }

                // Text before cursor.
                Text {
                    id: textBefore
                    text: cmdlineState.text.substring(0, cmdlineState.cursorPos)
                    color: Theme.color.text.emphasis
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.md
                    anchors.verticalCenter: cmdRow.verticalCenter
                    renderType: Text.NativeRendering
                }

                // 2px bar cursor. Simpler than block-over-char and avoids
                // having to measure the single character at cursorPos.
                Rectangle {
                    width: 2
                    height: Theme.font.size.md + 4
                    color: Theme.color.text.emphasis
                    anchors.verticalCenter: textBefore.verticalCenter
                }

                // Text after cursor.
                Text {
                    text: cmdlineState.text.substring(cmdlineState.cursorPos)
                    color: Theme.color.text.emphasis
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.md
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
            height: Math.min(root.maxPopupRows, popupList.count) * root.rowHeight + 2 * root.popupInset
            radius: Theme.radius.md
            // Same raised rung as the cmdline box above — this popup floats
            // over the same chrome.
            color: Theme.color.bg.raised
            border.color: Theme.color.border.hairline
            border.width: 1
            clip: true

            ListView {
                id: popupList
                anchors.fill: parent
                anchors.margins: root.popupInset
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
                    color: ListView.isCurrentItem ? Theme.color.bg.raisedSelected : "transparent"
                    radius: Theme.radius.sm

                    Text {
                        anchors.fill: delegateRow
                        anchors.leftMargin: Theme.spacing.md
                        anchors.rightMargin: Theme.spacing.md
                        verticalAlignment: Text.AlignVCenter
                        text: model.word
                        color: ListView.isCurrentItem ? Theme.color.text.selected : Theme.color.text.normal
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        elide: Text.ElideRight
                        renderType: Text.NativeRendering
                    }
                }
            }
        }
    }
}
