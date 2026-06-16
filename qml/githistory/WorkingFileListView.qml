// Working-file list — the "master" pane of the uncommitted-files sub-view.
//
// The Tab-toggle counterpart to CommitListView: where that walks committed
// history, this walks the UNCOMMITTED working tree. A keyboard-driven ListView
// over the injected `model` (the live `GitStatusListModel` — same flat
// per-file status the Active Changes side panel renders, already kept current
// by the worktree watcher, so this view never scans anything itself). Selection
// -move drives the detail pane's diff: j/k walk the changed files and the diff
// for the highlighted file streams in live — the comprehension flow the whole
// surface exists for, applied to "what haven't I committed yet" instead of
// "how did we get here".
//
// Structurally parallel to CommitListView ON PURPOSE — the clay selection pill,
// the j/k/gg/G/Ctrl+D/Ctrl+U key handling, and the `currentItem`-surfaced
// `currentFile` are the same idiom. They stay separate components because the
// delegates and backing models differ; the shared *rendering* (the diff) is
// what got extracted (DiffView), not the navigation shell. Keep the two nav
// blocks in sync if either changes.
//
// Bound to an INJECTED model (set by GitHistoryView from the `gitStatusList`
// context property), never a global — so the subtree stays extraction-ready
// for a future Symmetria.Git.UI module alongside the commit views.

import QtQuick
import QtQuick.Layouts
import ".."
import "../design"

FocusScope {
    id: root

    // Injected from the host — the flat GitStatusListModel (`gitStatusList`).
    required property var model

    // Emitted when the user presses Tab/Backtab — the host (GitHistoryView)
    // toggles between this uncommitted-files view and the commit-history view.
    // Captured here (where the focused ListView lives and already owns
    // Keys.onPressed) because Tab is Qt's focus-traversal key: intercepting it
    // at the focus point and accepting the event is the only reliable way to
    // repurpose it without the event first moving focus between items.
    signal toggleRequested()

    // The highlighted file's data, surfaced for the detail pane. Null when the
    // working tree is clean (no rows). `untracked` is pre-derived so the host
    // can pick the right diff command (HEAD diff vs --no-index) without
    // re-deriving it from the state string.
    readonly property var currentFile: view.currentItem
        ? ({
            path: view.currentItem.path,
            displayName: view.currentItem.displayName,
            statusChar: view.currentItem.statusChar,
            statusState: view.currentItem.statusState,
            tooltip: view.currentItem.tooltip,
            additions: view.currentItem.additions,
            deletions: view.currentItem.deletions,
            untracked: view.currentItem.statusState === "untracked",
            // Resolved badge colour, computed once here so the detail pane
            // doesn't re-derive the same state→colour map (one source of truth).
            stateColor: root._stateColor(view.currentItem.statusState),
        })
        : null

    // Re-request focus onto the list (host calls this when this view shows).
    function focusList(): void {
        view.forceActiveFocus();
    }

    // Map a git-status state to its badge colour. Theme-only (no FmTheme
    // import) so the subtree stays self-contained: staged reads as "added"
    // green, unstaged/conflicted as "removed" red, untracked as the brighter
    // accent, renamed as the base accent. Mirrors the semantic split the
    // Active Changes side panel gets from FmTheme.gitStatus.*, expressed in
    // this surface's own tokens.
    function _stateColor(state: string): color {
        if (state === "staged")
            return Theme.color.diff.addedFg;
        if (state === "unstaged" || state === "conflicted")
            return Theme.color.diff.removedFg;
        if (state === "untracked")
            return Theme.color.accent.bright;
        if (state === "renamed")
            return Theme.color.accent.primary;
        return Theme.color.text.normal;
    }

    // Fixed delegate row height — shared by the delegate and the Ctrl+D/U
    // half-page math so the two can't drift. Matches CommitListView's rhythm
    // so the inset clay selection pill breathes identically across both views.
    readonly property int rowHeight: 34
    readonly property int _halfPage: Math.max(1, Math.floor(view.height / (2 * root.rowHeight)))
    property bool _gPending: false

    Timer {
        id: gReset
        interval: 400
        onTriggered: root._gPending = false
    }

    ListView {
        id: view
        anchors.fill: parent
        clip: true
        focus: true
        model: root.model
        currentIndex: 0
        highlightMoveDuration: 0
        boundsBehavior: Flickable.StopAtBounds
        cacheBuffer: 600
        // Shadow clearance before the clip edge, same as CommitListView — the
        // clay pill's shadow renders OUTSIDE the pill and the ListView clips.
        topMargin: Theme.spacing.xs
        bottomMargin: Theme.spacing.xs

        // The status model fully resets on each worktree scan. Re-seed the
        // selection to the first row whenever it lands on an invalid index
        // (Qt clears currentIndex to -1 across some resets) so a diff
        // auto-loads without the user pressing anything.
        Connections {
            target: root.model
            function onCountChanged(): void {
                if (view.count > 0 && view.currentIndex < 0)
                    view.currentIndex = 0;
            }
        }

        Keys.onPressed: function (event) {
            // Tab / Backtab → ask the host to switch sub-views. Accept the
            // event so Qt's focus traversal doesn't also fire.
            if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
                root.toggleRequested();
                event.accepted = true;
                return;
            }
            // Any key other than a bare `g` cancels a pending `gg`.
            if (event.key !== Qt.Key_G || (event.modifiers & Qt.ShiftModifier)) {
                root._gPending = false;
                gReset.stop();
            }
            switch (event.key) {
            case Qt.Key_J:
                view.incrementCurrentIndex();
                event.accepted = true;
                break;
            case Qt.Key_K:
                view.decrementCurrentIndex();
                event.accepted = true;
                break;
            case Qt.Key_G:
                if (event.modifiers & Qt.ShiftModifier) {
                    view.currentIndex = view.count - 1;
                } else if (root._gPending) {
                    root._gPending = false;
                    gReset.stop();
                    view.currentIndex = 0;
                } else {
                    root._gPending = true;
                    gReset.restart();
                }
                event.accepted = true;
                break;
            case Qt.Key_D:
                if (event.modifiers & Qt.ControlModifier) {
                    view.currentIndex = Math.min(view.count - 1,
                                                 view.currentIndex + root._halfPage);
                    event.accepted = true;
                }
                break;
            case Qt.Key_U:
                if (event.modifiers & Qt.ControlModifier) {
                    view.currentIndex = Math.max(0, view.currentIndex - root._halfPage);
                    event.accepted = true;
                }
                break;
            }
        }

        delegate: Item {
            id: rowItem
            // Role bindings from GitStatusListModel (`path` = absolute path,
            // `displayName` = repo-relative). `currentFile` reads these
            // directly off `view.currentItem`.
            required property string path
            required property string displayName
            required property string statusChar
            required property string statusState
            required property string tooltip
            required property int additions
            required property int deletions
            required property int index

            readonly property bool isCurrent: ListView.isCurrentItem

            width: ListView.view.width
            height: root.rowHeight

            // CLAY selection capsule — identical idiom to CommitListView: the
            // current file raises into a clay pill, every other row stays flat.
            PillSurface {
                anchors.fill: parent
                anchors.topMargin: Theme.spacing.xxs
                anchors.bottomMargin: Theme.spacing.xxs
                anchors.leftMargin: Theme.spacing.xs
                anchors.rightMargin: Theme.spacing.xs
                radius: Theme.radius.sm
                elevated: rowItem.isCurrent
                color: rowItem.isCurrent ? Theme.color.bg.selected : "transparent"
                borderColor: rowItem.isCurrent ? Theme.color.border.hairline : "transparent"
            }

            MouseArea {
                anchors.fill: parent
                onClicked: {
                    view.currentIndex = rowItem.index;
                    view.forceActiveFocus();
                }
            }

            RowLayout {
                anchors.left: parent.left
                anchors.leftMargin: Theme.spacing.md
                anchors.right: parent.right
                anchors.rightMargin: Theme.spacing.md
                anchors.verticalCenter: parent.verticalCenter
                spacing: Theme.spacing.sm

                // Status badge — single char tinted by state.
                Text {
                    Layout.preferredWidth: 14
                    Layout.alignment: Qt.AlignVCenter
                    text: rowItem.statusChar
                    color: root._stateColor(rowItem.statusState)
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    font.weight: Theme.font.weight.bold
                    horizontalAlignment: Text.AlignHCenter
                    renderType: Text.NativeRendering
                }

                // Repo-relative path — elide LEFT so the filename (the
                // most-identifying tail) stays visible when the pane narrows.
                Text {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                    text: rowItem.displayName
                    color: rowItem.isCurrent ? Theme.color.text.strong : Theme.color.text.normal
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    elide: Text.ElideLeft
                    renderType: Text.NativeRendering
                }

                // +adds (only when non-zero — binary / untracked-binary rows
                // legitimately carry 0/0).
                Text {
                    visible: rowItem.additions > 0
                    Layout.alignment: Qt.AlignVCenter
                    text: "+" + rowItem.additions
                    color: Theme.color.diff.addedFg
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    renderType: Text.NativeRendering
                }

                // −dels
                Text {
                    visible: rowItem.deletions > 0
                    Layout.alignment: Qt.AlignVCenter
                    text: "-" + rowItem.deletions
                    color: Theme.color.diff.removedFg
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    renderType: Text.NativeRendering
                }
            }
        }
    }

    // Empty state — the working tree is clean (nothing uncommitted). Calm,
    // not an error: this is the "all committed" resting state. Tab still
    // switches to history from here (the ListView keeps focus when empty).
    Text {
        anchors.centerIn: parent
        visible: !root.model || root.model.count === 0
        text: "working tree clean"
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.sm
        renderType: Text.NativeRendering
    }
}
