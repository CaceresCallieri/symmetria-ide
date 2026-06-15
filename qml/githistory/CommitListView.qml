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
import "../design"

FocusScope {
    id: root

    // Injected from the host (Main.qml binds `gitLogModel`).
    required property var model
    // Injected controller — used only for `load_more` / `hasMore` paging.
    property var controller: null

    // The highlighted row's data, surfaced for the detail pane. Null when the
    // list is empty (clean reset / no repo). Re-evaluates whenever the
    // ListView's currentItem swaps, which fires `currentCommitChanged`.
    readonly property var currentCommit: view.currentItem
        ? ({
            hash: view.currentItem.cHash,
            abbrevHash: view.currentItem.cAbbrev,
            subject: view.currentItem.cSubject,
            body: view.currentItem.cBody,
            authorName: view.currentItem.cAuthor,
            authorEmail: view.currentItem.cEmail,
            dateIso: view.currentItem.cDateIso,
            relativeDate: view.currentItem.cRelative,
            refs: view.currentItem.cRefs,
            agentId: view.currentItem.cAgentId,
        })
        : null

    // Re-request focus onto the list (host calls this when the surface shows).
    function focusList(): void {
        view.forceActiveFocus();
    }

    // Half-page jump distance for Ctrl+D / Ctrl+U, derived from the viewport.
    readonly property int _halfPage: Math.max(1, Math.floor(view.height / (2 * 30)))
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

        // After a reset (repo switch / fresh load) the model repopulates from
        // empty; point the selection back at the newest commit so its diff
        // auto-loads without the user pressing anything.
        Connections {
            target: root.model
            function onCountChanged(): void {
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
            }
        }

        delegate: Rectangle {
            id: rowItem
            // Role bindings (a C++ model exposes roles as required properties).
            // The `c`-prefixed aliases below are what `currentCommit` reads.
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

            readonly property string cHash: hash
            readonly property string cAbbrev: abbrevHash
            readonly property string cSubject: subject
            readonly property string cBody: body
            readonly property string cAuthor: authorName
            readonly property string cEmail: authorEmail
            readonly property string cDateIso: dateIso
            readonly property string cRelative: relativeDate
            readonly property string cRefs: refs
            readonly property string cAgentId: agentId

            readonly property bool isCurrent: ListView.isCurrentItem

            width: ListView.view.width
            height: 30
            color: isCurrent ? Theme.color.bg.selected : "transparent"

            // Left accent bar marks the selected row (calm, not a full fill).
            Rectangle {
                width: 2
                height: parent.height
                color: rowItem.isCurrent ? Theme.color.accent.primary : "transparent"
            }

            MouseArea {
                anchors.fill: parent
                onClicked: {
                    view.currentIndex = rowItem.index;
                    view.forceActiveFocus();
                }
            }

            Row {
                anchors.left: parent.left
                anchors.leftMargin: Theme.spacing.md
                anchors.right: parent.right
                anchors.rightMargin: Theme.spacing.md
                anchors.verticalCenter: parent.verticalCenter
                spacing: Theme.spacing.sm

                Text {
                    width: 58
                    text: rowItem.abbrevHash
                    color: Theme.color.accent.primary
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    elide: Text.ElideRight
                    renderType: Text.NativeRendering
                    anchors.verticalCenter: parent.verticalCenter
                }

                // Ref badge (branch / tag pointers) — only when present.
                Rectangle {
                    visible: rowItem.refs.length > 0
                    width: refLabel.implicitWidth + Theme.spacing.sm
                    height: refLabel.implicitHeight + Theme.spacing.xxs * 2
                    radius: Theme.radius.sm
                    color: "transparent"
                    border.color: Theme.color.accent.bright
                    border.width: 1
                    anchors.verticalCenter: parent.verticalCenter
                    Text {
                        id: refLabel
                        anchors.centerIn: parent
                        text: rowItem.refs
                        color: Theme.color.accent.bright
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        renderType: Text.NativeRendering
                    }
                }

                Text {
                    width: parent.width - 58 - relDate.width - Theme.spacing.sm * 3
                           - (rowItem.refs.length > 0 ? refLabel.implicitWidth + Theme.spacing.sm * 2 : 0)
                    text: rowItem.subject
                    color: rowItem.isCurrent ? Theme.color.text.strong : Theme.color.text.normal
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    elide: Text.ElideRight
                    renderType: Text.NativeRendering
                    anchors.verticalCenter: parent.verticalCenter
                }

                Text {
                    id: relDate
                    text: rowItem.relativeDate
                    color: Theme.color.text.dim
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    renderType: Text.NativeRendering
                    anchors.verticalCenter: parent.verticalCenter
                }
            }

            // Hairline separator between rows.
            Rectangle {
                anchors.bottom: parent.bottom
                anchors.left: parent.left
                anchors.right: parent.right
                height: 1
                color: Theme.color.border.hairline
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
