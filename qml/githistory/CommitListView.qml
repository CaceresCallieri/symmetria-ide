// Commit list — the "master" pane of the git-history viewer.
//
// A keyboard-driven ListView over the injected `model` (a GitLogListModel
// exposed from Python). Selection-move is the primary interaction: j/k walk
// the history and the detail pane's diff updates live off `currentCommit` —
// the comprehension-first behaviour (arrow through commits, watch what each
// one changed) the whole surface exists to enable.
//
// `model` access constraint: a C++ QAbstractListModel exposes no `get(i)` to
// QML, so the detail pane cannot read an arbitrary row. The delegate is the
// only place role data is live — it declares a `required property` per role,
// and this view surfaces the highlighted row outward via `currentCommit`.
//
// Bound to an INJECTED model (set by Main.qml), never a global — so this
// subtree stays extraction-ready for a future Symmetria.Git.UI module.

import QtQuick
import QtQuick.Layouts
import ".."
import "../design"

FocusScope {
    id: root

    // Injected from the host (Main.qml binds `gitLogModel`).
    required property var model
    // Injected controller — used only for `load_more` / `hasMore` paging.
    property var controller: null

    // Emitted when the user presses Tab/Backtab — the host (GitHistoryView)
    // toggles between this commit-history view and the uncommitted-files view.
    // Captured in the ListView's Keys.onPressed (Tab is Qt's focus-traversal
    // key, so it must be intercepted and accepted at the focus point).
    signal toggleRequested()

    // Remote-sync intents — pure intents (this view holds no controller for
    // them), routed by the host (GitHistoryView) to GitOpsController.pull() /
    // a push-confirm dialog. `p` pulls, `P` (Shift) pushes — keyboard-first
    // git sync from the history view, the surface's first mutating actions.
    signal pullRequested()
    signal pushRequested()
    // Esc dismisses a stuck git-ops status toast (the persistent error case).
    // Bubbled to the host → Main.qml hides the toast.
    signal dismissStatusRequested()

    // The highlighted row's data, surfaced for the detail pane. Null when the
    // list is empty (clean reset / no repo). Re-evaluates whenever the
    // ListView's currentItem swaps, which fires `currentCommitChanged`.
    readonly property var currentCommit: view.currentItem
        ? ({
            hash: view.currentItem.hash,
            abbrevHash: view.currentItem.abbrevHash,
            subject: view.currentItem.subject,
            body: view.currentItem.body,
            authorName: view.currentItem.authorName,
            authorEmail: view.currentItem.authorEmail,
            dateIso: view.currentItem.dateIso,
            relativeDate: view.currentItem.relativeDate,
            refs: view.currentItem.refs,
            agentId: view.currentItem.agentId,
        })
        : null

    // Re-request focus onto the list (host calls this when the surface shows).
    function focusList(): void {
        view.forceActiveFocus();
    }

    // Fixed delegate row height — shared by the delegate and the Ctrl+D/U
    // half-page math so the two can't drift out of sync. Roomier than a
    // terminal row so the inset clay selection pill breathes (FM rhythm).
    readonly property int rowHeight: 34
    // Half-page jump distance for Ctrl+D / Ctrl+U, derived from the viewport.
    readonly property int _halfPage: Math.max(1, Math.floor(view.height / (2 * root.rowHeight)))
    // `gg` pending flag — set by the first `g`, consumed by the second, and
    // cleared after a short window so a lone `g` doesn't arm forever.
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
        // Keep the highlighted row comfortably in view as j/k walks it.
        highlightMoveDuration: 0
        boundsBehavior: Flickable.StopAtBounds
        cacheBuffer: 600
        // Small content inset inside the clipped viewport so the first/last
        // selected clay pill has room for its shadow before the clip edge
        // (the ListView clips; clay shadows render OUTSIDE the pill — see the
        // PillSurface gotcha note).
        topMargin: Theme.spacing.xs
        bottomMargin: Theme.spacing.xs

        // After a reset (repo switch / fresh load) the model repopulates from
        // empty; point the selection back at the newest commit so its diff
        // auto-loads without the user pressing anything.
        Connections {
            target: root.model
            function onCountChanged(): void {
                // `currentIndex < 0` is the reset-vs-append discriminator: a
                // beginResetModel clears currentIndex to -1 (so we re-seed to
                // the newest commit), whereas a beginInsertRows "load more"
                // append preserves it (>= 0), so the guard leaves the user's
                // selection untouched. Do not "simplify" this condition away.
                if (view.count > 0 && view.currentIndex < 0)
                    view.currentIndex = 0;
            }
        }

        // Page in older commits as the selection nears the tail.
        onCurrentIndexChanged: {
            if (root.controller
                    && root.controller.hasMore
                    && view.currentIndex >= view.count - 8)
                root.controller.load_more();
        }

        Keys.onPressed: function (event) {
            // Tab / Backtab → ask the host to switch sub-views. Accept the
            // event so Qt's focus traversal doesn't also fire.
            if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
                root.toggleRequested();
                event.accepted = true;
                return;
            }
            // Any key other than a bare `g` cancels a pending `gg` (vim
            // semantics — an interleaved motion must not let a later `g`
            // complete a stale `gg` jump).
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
                    // G — jump to the oldest loaded commit.
                    view.currentIndex = view.count - 1;
                } else if (root._gPending) {
                    // gg — jump to the newest.
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
            case Qt.Key_P:
                // p → pull, P (Shift) → push. Pure intents; the host routes
                // pull straight to GitOpsController and push through a confirm.
                if (event.modifiers & Qt.ShiftModifier)
                    root.pushRequested();
                else
                    root.pullRequested();
                event.accepted = true;
                break;
            case Qt.Key_Escape:
                // Dismiss a stuck git-ops error toast. Harmless no-op when none
                // is showing (the host just calls hide() on an already-hidden
                // toast). The git surface has no other Esc behaviour to swallow.
                root.dismissStatusRequested();
                event.accepted = true;
                break;
            }
        }

        delegate: Item {
            id: rowItem
            // Role bindings — a C++ model exposes roles as required properties.
            // `currentCommit` reads these directly off `view.currentItem`.
            required property string hash
            required property string abbrevHash
            required property string subject
            required property string body
            required property string authorName
            required property string authorEmail
            required property string dateIso
            required property string relativeDate
            required property string refs
            required property string agentId
            // Model index — used for click selection.
            required property int index

            readonly property bool isCurrent: ListView.isCurrentItem

            width: ListView.view.width
            height: root.rowHeight

            // CLAY selection capsule — the current commit raises into a clay
            // pill (matte fill + hairline border + convex depth), while every
            // other row stays fully flat (transparent fill + transparent
            // border, no shadow). This is the File Manager active-tab /
            // surface-switcher idiom: `elevated` gates the depth so only the
            // focal commit floats, keeping the dense log readable. Inset a few
            // px so the pill floats with breathing room and casts its shadow
            // into the gap (matching the FM's spaced rows) rather than
            // edge-to-edge. Declared FIRST so it renders behind the content
            // Row + MouseArea.
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

            // RowLayout (not a manual-width Row): the subject takes the
            // remaining space via fillWidth and elides — it can never be
            // computed to a negative width and vanish, which the old
            // arithmetic risked when a long ref list met a narrow pane.
            RowLayout {
                anchors.left: parent.left
                anchors.leftMargin: Theme.spacing.md
                anchors.right: parent.right
                anchors.rightMargin: Theme.spacing.md
                anchors.verticalCenter: parent.verticalCenter
                spacing: Theme.spacing.sm

                Text {
                    Layout.preferredWidth: 58
                    Layout.alignment: Qt.AlignVCenter
                    text: rowItem.abbrevHash
                    color: Theme.color.accent.primary
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    elide: Text.ElideRight
                    renderType: Text.NativeRendering
                }

                // Ref badge (branch / tag pointers) — only when present. Capped
                // so a long ref list can't crowd out the subject; the label
                // elides inside the cap.
                Rectangle {
                    visible: rowItem.refs.length > 0
                    Layout.preferredWidth: visible ? refLabel.implicitWidth + Theme.spacing.sm : 0
                    Layout.maximumWidth: 160
                    Layout.preferredHeight: refLabel.implicitHeight + Theme.spacing.xxs * 2
                    Layout.alignment: Qt.AlignVCenter
                    radius: Theme.radius.sm
                    color: "transparent"
                    border.color: Theme.color.accent.bright
                    border.width: 1
                    Text {
                        id: refLabel
                        anchors.fill: parent
                        anchors.leftMargin: Theme.spacing.xs
                        anchors.rightMargin: Theme.spacing.xs
                        verticalAlignment: Text.AlignVCenter
                        text: rowItem.refs
                        color: Theme.color.accent.bright
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        elide: Text.ElideRight
                        renderType: Text.NativeRendering
                    }
                }

                Text {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                    text: rowItem.subject
                    color: rowItem.isCurrent ? Theme.color.text.strong : Theme.color.text.normal
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    elide: Text.ElideRight
                    renderType: Text.NativeRendering
                }

                Text {
                    Layout.alignment: Qt.AlignVCenter
                    text: rowItem.relativeDate
                    color: Theme.color.text.dim
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    renderType: Text.NativeRendering
                }
            }
        }
    }

    // Empty state — no repo or no commits.
    Text {
        anchors.centerIn: parent
        visible: view.count === 0
        text: "no commit history"
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.sm
        renderType: Text.NativeRendering
    }
}
