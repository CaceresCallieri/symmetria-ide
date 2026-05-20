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

    // Header-bucket aggregates exposed by `GitController.stats`
    // (QVariantMap). Each bucket carries adds / dels / file-count; the
    // header repeater binds against these. Default `{}` lets us render
    // gracefully on first paint before the worker has produced numbers
    // (every `modelData.n > 0` guard handles the undefined case).
    property var stats: ({})

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
    // Include the asymmetric top+bottom margins so the chrome Rectangle
    // matches the actual content layout. Without the `+ topMargin +
    // bottomMargin`, ColumnLayout inside this Item gets anchors.fill with
    // margins which leaves it `2*margin` less tall than its content wants,
    // squeezing the last row.
    implicitHeight: visible
        ? content.implicitHeight + Theme.spacing.sm * 2
        : 0
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
        // Asymmetric margins: tighter on the sides (matches the file tree
        // below for column-aligned reading) and looser top/bottom so the
        // panel reads as a distinct card. The extra bottom space, combined
        // with the outer `ColumnLayout.spacing: lg` in Main.qml, gives the
        // panel visible "footer" breathing room before the tree starts.
        anchors.topMargin: Theme.spacing.sm
        anchors.bottomMargin: Theme.spacing.sm
        anchors.leftMargin: Theme.spacing.xs
        anchors.rightMargin: Theme.spacing.xs
        spacing: Theme.spacing.xs

        // Section header — quiet label + three aggregate bucket rows
        // (staged ●, unstaged ○, untracked ✦) carrying +adds -dels (n).
        // The icons + colour mapping reuse `_colorForState` so the
        // header reads continuous with the per-row badge palette below.
        // Bucket rows hide themselves when their file count is 0, so a
        // clean staging area collapses to just the title.
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 2

            Text {
                Layout.fillWidth: true
                text: "Changes" + (root.model ? " · " + root.model.count : "")
                color: Theme.color.text.dim
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.xs
            }

            Repeater {
                model: [
                    {icon: "●", stateName: "staged",
                     add: (root.stats && root.stats.stagedAdd) || 0,
                     del: (root.stats && root.stats.stagedDel) || 0,
                     n:   (root.stats && root.stats.stagedFiles) || 0},
                    {icon: "○", stateName: "unstaged",
                     add: (root.stats && root.stats.unstagedAdd) || 0,
                     del: (root.stats && root.stats.unstagedDel) || 0,
                     n:   (root.stats && root.stats.unstagedFiles) || 0},
                    {icon: "✦", stateName: "untracked",
                     add: (root.stats && root.stats.untrackedLines) || 0,
                     del: 0,
                     n:   (root.stats && root.stats.untrackedCount) || 0},
                ]
                delegate: RowLayout {
                    visible: modelData.n > 0
                    spacing: Theme.spacing.xs

                    Text {
                        text: modelData.icon
                        color: _colorForState(modelData.stateName)
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                    }
                    Text {
                        text: "+" + modelData.add
                        color: Theme.color.diff.addedFg
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                    }
                    Text {
                        visible: modelData.del > 0
                        text: "-" + modelData.del
                        color: Theme.color.diff.removedFg
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                    }
                    Text {
                        text: "(" + modelData.n + ")"
                        color: Theme.color.text.dim
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                    }
                    // Soak up remaining space so the row left-aligns
                    // tightly rather than spreading across the width.
                    Item { Layout.fillWidth: true }
                }
            }
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

                    // Per-row line delta. Tinted with the same diff palette
                    // as the header buckets so the visual grammar matches.
                    // Hidden when both numbers are 0 (binary file, rename
                    // mismatch, or a file whose row state doesn't have a
                    // matching numstat entry) — silent failure mode by
                    // contract; the file row still renders.
                    RowLayout {
                        Layout.alignment: Qt.AlignRight | Qt.AlignVCenter
                        visible: model.additions > 0 || model.deletions > 0
                        spacing: Theme.spacing.xs

                        Text {
                            visible: model.additions > 0
                            text: "+" + model.additions
                            color: Theme.color.diff.addedFg
                            font.family: Theme.font.family
                            font.pixelSize: Theme.font.size.xs
                        }
                        Text {
                            visible: model.deletions > 0
                            text: "-" + model.deletions
                            color: Theme.color.diff.removedFg
                            font.family: Theme.font.family
                            font.pixelSize: Theme.font.size.xs
                        }
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
