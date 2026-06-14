// Native which-key overlay.
//
// Bottom-anchored panel (above the StatusBar, below the editor) that
// mirrors the user's reference layout: a grid of `<key> → [icon] desc`
// entries arranged into columns, with an ESC/<BS> footer. Data is entirely
// driven by `whichKeyModel` + `whichKeyState`; the emitter lives in
// `runtime/lua/orchestrator/whichkey/init.lua` and the model/state
// wiring is in `src/symmetria_ide/app.py`.
//
// Visibility is `whichKeyState.visible` — no timers, no mouse, no
// focus transfer. Keystrokes flow to NeoVim; Lua decides when to emit
// show/hide.
//
// Layout strategy: a fixed number of columns, items poured top-to-bottom
// within each column (column-major order, matching which-key's default).
// Column count = floor(width / DESIRED_COL_WIDTH). Row height fixed so
// the panel's implicit height is `rowsPerColumn * rowHeight + footer`.
//
// All color and typography values bind against the `Theme` singleton
// (`qml/design/Theme.qml`). Local literals belong in Theme, not here.

import QtQuick

import "design"

Rectangle {
    id: root

    color: Theme.color.bg.chrome
    border.color: Theme.color.border.hairline
    border.width: 1
    radius: Theme.radius.lg

    // --- Layout math. Kept as properties so bindings stay reactive.
    property int desiredColWidth: 224
    property int horizontalPadding: Theme.spacing.lg
    property int verticalPadding: Theme.spacing.md
    property int rowHeight: Theme.size.whichKeyRowHeight
    property int footerHeight: Theme.size.whichKeyFooterHeight
    // Reserved horizontal budget per row for the key label, arrow, and spacing cells.
    // = key label width (18) + spacing (Theme.spacing.sm×2 = 12) + arrow glyph (~12) + spacing (Theme.spacing.sm = 6) + border = ~52
    // Adjust if key label width or row spacing changes.
    property int keyColumnReserved: 52
    // Extra width consumed by the icon slot when a glyph is present.
    property int iconSlotWidth: 20

    property int innerWidth: Math.max(0, width - 2 * horizontalPadding)
    property int columnCount: Math.max(1, Math.floor(innerWidth / desiredColWidth))
    property int columnWidth: columnCount > 0 ? Math.floor(innerWidth / columnCount) : innerWidth
    property int itemCount: whichKeyModel.rowCount()
    property int rowsPerColumn: Math.max(1, Math.ceil(itemCount / columnCount))

    // Panel's natural height — parent clamps to a max so huge menus
    // don't swallow the viewport. v1: no scroll; excess items are just
    // cut off by the clip at the content bottom. Scroll lands post-v1.
    implicitHeight: rowsPerColumn * rowHeight + footerHeight + 2 * verticalPadding

    visible: whichKeyState.visible
    clip: true

    // `whichKeyModel.rowCount()` is a function call, not a bindable
    // property. Re-seed `itemCount` via a Connection so the layout
    // rebinds when the model resets. See CLAUDE.md gotcha #3.
    //
    // Only `onModelReset` is wired here because WhichKeyModel always performs
    // full item replacement via beginResetModel/endResetModel — it never emits
    // rowsInserted or rowsRemoved. If that ever changes, add the corresponding
    // handlers here to keep itemCount in sync.
    Connections {
        target: whichKeyModel
        function onModelReset() {
            root.itemCount = whichKeyModel.rowCount()
        }
    }

    // --- Entry grid. Each delegate positions itself by index into a
    // column-major order: column = floor(i / rowsPerColumn), row = i % rowsPerColumn.
    Item {
        id: content
        anchors.left: root.left
        anchors.right: root.right
        anchors.top: root.top
        anchors.leftMargin: root.horizontalPadding
        anchors.rightMargin: root.horizontalPadding
        anchors.topMargin: root.verticalPadding
        height: root.rowsPerColumn * root.rowHeight

        Repeater {
            model: whichKeyModel
            delegate: Item {
                id: entry
                width: root.columnWidth
                height: root.rowHeight
                x: Math.floor(index / root.rowsPerColumn) * root.columnWidth
                y: (index % root.rowsPerColumn) * root.rowHeight

                Row {
                    anchors.verticalCenter: entry.verticalCenter
                    spacing: Theme.spacing.sm

                    // Key label. Fixed minimum width so arrows align
                    // across rows within a column.
                    Text {
                        width: 18
                        horizontalAlignment: Text.AlignLeft
                        text: model.key
                        color: Theme.color.text.strong
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        renderType: Text.NativeRendering
                    }

                    Text {
                        text: "→"
                        color: Theme.color.accent.primary
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        renderType: Text.NativeRendering
                    }

                    // Icon slot. Absent → empty string, collapses the
                    // horizontal run so descriptions still align
                    // reasonably within the column.
                    Text {
                        visible: model.icon !== ""
                        text: model.icon
                        color: model.iconColor !== "" ? model.iconColor : Theme.color.text.emphasis
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        renderType: Text.NativeRendering
                    }

                    Text {
                        // Groups render with a leading `+` (matches
                        // which-key convention the user knows by sight).
                        text: (model.isGroup ? "+" : "") + model.desc
                        color: model.isGroup ? Theme.color.accent.bright : Theme.color.text.emphasis
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        elide: Text.ElideRight
                        // Cap to remaining column width so long descriptions
                        // don't overflow into the next column.
                        width: Math.max(0, root.columnWidth - root.keyColumnReserved
                               - (model.icon !== "" ? root.iconSlotWidth : 0))
                        renderType: Text.NativeRendering
                    }
                }
            }
        }
    }

    // --- Footer: ESC close / <BS> back hints.
    Row {
        id: footer
        anchors.horizontalCenter: root.horizontalCenter
        anchors.bottom: root.bottom
        anchors.bottomMargin: Theme.spacing.sm
        spacing: Theme.spacing.lg
        height: root.footerHeight

        Row {
            spacing: Theme.spacing.xs
            // footer is the outer Row (immediate parent) — explicit id
            // reference instead of parent, per §3 P2 id-naming convention.
            anchors.verticalCenter: footer.verticalCenter
            Text {
                text: "ESC"
                color: Theme.color.text.strong
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.xs
                font.weight: Theme.font.weight.medium
                renderType: Text.NativeRendering
            }
            Text {
                text: "close"
                color: Theme.color.text.dim
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.xs
                renderType: Text.NativeRendering
            }
        }

        Row {
            visible: whichKeyState.canGoBack
            spacing: Theme.spacing.xs
            anchors.verticalCenter: footer.verticalCenter
            Text {
                text: "⌫"
                color: Theme.color.text.strong
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.xs
                font.weight: Theme.font.weight.medium
                renderType: Text.NativeRendering
            }
            Text {
                text: "back"
                color: Theme.color.text.dim
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.xs
                renderType: Text.NativeRendering
            }
        }
    }
}
