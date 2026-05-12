// Active Changes panel — flat list of files with pending git changes.
//
// Sits ABOVE the FileTreeView in the side panel column. Auto-hidden when
// the working tree is clean or we're not in a git repo (model.count == 0).
// Each row shows a small status badge (M/A/D/?/U/R/C) and the file's
// repo-relative path. Clicking a row opens that file in nvim — the
// panel's `fileActivated` signal carries the absolute path, which
// Main.qml routes to `controller.open_in_nvim(path)` (same path the
// FileTreeView uses for `onFileActivated`).
//
// Visual hierarchy: borrows the same chrome palette (`Theme.color.bg.chrome`,
// `Theme.color.text.normal`, hairline border) as the StatusBar and other
// chrome panes, so it feels continuous with the surrounding UI. Badge
// colours come from `FmUi.FmTheme.gitStatus.*` — the same palette the
// tree's per-row badges use, so the two views are visually unified.

import QtQuick
import QtQuick.Layouts
import Symmetria.FileManager.UI as FmUi
import "design"

Item {
    id: root

    // Model — driven by `gitStatusList` context property (a
    // GitStatusListModel projecting GitController's map). Set at use
    // site so this component stays portable / reusable.
    property QtObject model: null

    // Emitted when the user clicks a row. Carries the ABSOLUTE filesystem
    // path of the activated file. Main.qml connects this to
    // `controller.open_in_nvim(path)` and then re-focuses the editor —
    // mirrors the FM's `FileTreeView.onFileActivated` contract so the
    // two surfaces feel identical from the user's perspective.
    signal fileActivated(string absolutePath)

    // Auto-hide when there are no changes. Hidden state collapses the
    // vertical real estate so the file tree below claims it back — this
    // is the "same panel, two sections" composition behaviour: when
    // changes are 0, the section vanishes; the tree expands naturally.
    visible: model && model.count > 0
    implicitHeight: visible ? content.implicitHeight : 0
    Layout.preferredHeight: implicitHeight
    Layout.fillWidth: true

    // The visible bounds match Theme's chrome — same matte tone the
    // status bar and which-key overlay use. Drops slightly darker to
    // separate visually from the (transparent) file tree below.
    Rectangle {
        anchors.fill: parent
        color: Theme.color.bg.chrome
        border.width: 1
        border.color: Theme.color.border.hairline
    }

    ColumnLayout {
        id: content
        anchors.fill: parent
        anchors.margins: Theme.spacing.xs
        spacing: Theme.spacing.xs

        // Section header — quiet label so the panel reads as a labelled
        // bucket of changes rather than a generic list. `dim` text colour
        // keeps it secondary to the file rows themselves.
        Text {
            Layout.fillWidth: true
            text: "Changes" + (root.model ? " · " + root.model.count : "")
            color: Theme.color.text.dim
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.xs
        }

        // The list of changed files. Cap height to ~40% of available
        // space so a large changeset doesn't dominate the side panel and
        // crowd out the file tree below — anything beyond scrolls. The
        // exact ratio is intentionally generous (vs. e.g. 25%) because
        // we expect short changesets to be the common case; the cap only
        // bites on outlier huge changesets.
        ListView {
            id: changesList
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(
                contentHeight,
                Math.max(parent.height * 0.4, 120)
            )
            clip: true
            model: root.model
            spacing: 1
            boundsBehavior: Flickable.StopAtBounds

            delegate: Rectangle {
                id: rowBg
                width: ListView.view.width
                height: rowLayout.implicitHeight + Theme.spacing.xs * 2
                color: rowMouse.containsMouse
                    ? Theme.color.bg.selected
                    : "transparent"

                MouseArea {
                    id: rowMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    // FM brief contract: row click emits fileActivated with the
                    // absolute path. The path role is populated by
                    // GitStatusListModel from GitController._resolved_root +
                    // the repo-relative key, so it's safe to hand straight to
                    // nvim's `:edit`.
                    onClicked: root.fileActivated(model.path)
                }

                RowLayout {
                    id: rowLayout
                    anchors.fill: parent
                    anchors.leftMargin: Theme.spacing.sm
                    anchors.rightMargin: Theme.spacing.sm
                    spacing: Theme.spacing.sm

                    // Small badge pill — matches the FM tree badge palette
                    // by state name. Width fixed so paths align column-wise.
                    Rectangle {
                        Layout.preferredWidth: 16
                        Layout.preferredHeight: 14
                        radius: 3
                        color: _colorForState(model.statusState)
                        Text {
                            anchors.centerIn: parent
                            text: model.statusChar
                            color: FmUi.FmTheme.gitStatus.badgeText
                            font.family: Theme.font.family
                            font.pixelSize: Theme.font.size.xs
                            font.bold: true
                        }
                    }

                    Text {
                        Layout.fillWidth: true
                        text: model.displayName
                        color: Theme.color.text.normal
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.sm
                        elide: Text.ElideMiddle
                        // ElideMiddle keeps both the directory prefix and
                        // the filename visible for long paths — more useful
                        // than ElideRight which would only show the parent
                        // directory and chop the actual filename. Hover
                        // tooltip deferred — the panel is keyboard-first
                        // (non-negotiable #1); adding QtQuick.Controls just
                        // for a mouse-only affordance isn't justified yet.
                    }
                }
            }
        }
    }

    // Local helper duplicating gitProviderAdapter's mapping. Could be
    // factored into a shared singleton later, but for two call sites
    // (here + Main.qml's adapter) inline duplication beats a new file.
    function _colorForState(state) {
        switch (state) {
            case "unstaged":    return FmUi.FmTheme.gitStatus.unstagedRed
            case "staged":      return FmUi.FmTheme.gitStatus.stagedGreen
            case "untracked":   return FmUi.FmTheme.gitStatus.untrackedBlue
            case "renamed":     return FmUi.FmTheme.gitStatus.renamedYellow
            case "conflicted":  return FmUi.FmTheme.gitStatus.conflictedMagenta
            case "ignored":     return FmUi.FmTheme.gitStatus.ignoredGray
            default:            return FmUi.FmTheme.gitStatus.unstagedRed
        }
    }
}
