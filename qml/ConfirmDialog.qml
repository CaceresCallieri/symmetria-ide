// Keyboard-first confirmation modal — a careful "are you sure?" gate in
// front of a consequential, hard-to-undo action. Built on ModalOverlay,
// so it inherits the scrim + FM scale-pop entrance + focus-self-heal that
// the agent spawn menu uses; this file adds only the title/message/buttons
// and the confirm/cancel key map.
//
// Its first caller is the window-close guard (Hyprland Super+Q): closing
// the IDE reaps every terminal-agent and the editor session, so a stray
// kill chord shouldn't tear the workspace down instantly. Generic by
// design — title/message/confirmText are properties — so any future
// destructive action can reuse it.
//
// Keys: Enter activates the HIGHLIGHTED button (default = confirm, per the
// "Enter to confirm" contract); ←/→ or h/l or Tab move the highlight so a
// user can deliberately land on Cancel and Enter that instead; Esc always
// cancels (inherited from ModalOverlay). Buttons are also click-targets —
// keyboard remains fully sufficient (non-negotiable #1), mouse is a bonus.
//
// All colour / typography / motion values bind against `Theme`.

import QtQuick

import "design"

ModalOverlay {
    id: root

    panelWidth: 360
    z: 50 // above the agent modals (z 40) — close can fire over anything

    property string title: "Are you sure?"
    property string message: ""
    property string confirmText: "Confirm"
    property string cancelText: "Cancel"

    // 0 = confirm button highlighted (the Enter target by default), 1 =
    // cancel. Reset to 0 on every open() so a fresh prompt always offers
    // confirm-on-Enter regardless of where the last one was left.
    property int _highlight: 0

    signal confirmed()
    signal cancelled()

    // Esc (handled in ModalOverlay) and the Cancel button both funnel
    // through dismiss() → dismissed; map that single channel to cancelled.
    onDismissed: root.cancelled()

    // Override the base open() to reset the highlight before raising.
    function open() {
        root._highlight = 0;
        _show();
    }

    function _activate() {
        if (root._highlight === 1) {
            root.dismiss();          // → dismissed → cancelled
        } else {
            // Confirm path: hide directly, do NOT call dismiss(). dismiss()
            // emits dismissed → onDismissed → cancelled() → the host's
            // _restoreCentralFocus(), which would race the authorize_and_quit
            // teardown that onConfirmed kicks off. Keep these two channels
            // separate — only the cancel branch is a "dismissal".
            root.visible = false;
            root.confirmed();
        }
    }

    onKeyPressed: function (event) {
        switch (event.key) {
        case Qt.Key_Return:
        case Qt.Key_Enter:
            root._activate();
            break;
        case Qt.Key_Left:
        case Qt.Key_H:
            root._highlight = 0;
            break;
        case Qt.Key_Right:
        case Qt.Key_L:
            root._highlight = 1;
            break;
        case Qt.Key_Tab:
        case Qt.Key_Backtab:
            // Only two targets, so forward (Tab) and reverse (Backtab) are
            // the same toggle — both just flip to the other button.
            root._highlight = root._highlight === 0 ? 1 : 0;
            break;
        default:
            // Swallow everything else — modal (already accepted upstream).
            break;
        }
    }

    Text {
        width: parent.width
        text: root.title
        color: Theme.color.text.strong
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.sm
        font.weight: Theme.font.weight.bold
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.WordWrap
        renderType: Text.NativeRendering
    }

    Text {
        visible: root.message.length > 0
        width: parent.width
        text: root.message
        color: Theme.color.text.normal
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.xs
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.WordWrap
        renderType: Text.NativeRendering
    }

    // Breathing room between the message and the action row — the Column's
    // uniform sm gap alone read too tight at this seam.
    Item { width: 1; height: Theme.spacing.sm }

    Row {
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: Theme.spacing.md

        // Confirm (index 0) — destructive accent (editor "stop" red) so
        // the consequence reads at a glance; Cancel (index 1) — neutral
        // white. The highlighted (keyboard-selected) button glows with its
        // accent as both border and text over a subtle raised surface;
        // the unselected one stays a hairline ghost. A calm tint, not a
        // loud fill — on-brand with the Symmetria aesthetic.
        Repeater {
            model: [
                { idx: 0, label: root.confirmText, accent: Theme.color.mode.replace },
                { idx: 1, label: root.cancelText, accent: Theme.color.text.selected },
            ]

            delegate: Item {
                id: button
                required property var modelData
                readonly property bool highlighted: root._highlight === modelData.idx

                width: buttonLabel.implicitWidth + Theme.spacing.lg * 2
                height: buttonLabel.implicitHeight + Theme.spacing.sm * 2

                // The highlighted (keyboard-selected / hovered) button raises
                // into a clay pill — matte fill + its accent as the border +
                // convex depth — so the Enter target reads as physically
                // pressed-forward; the other stays a flat hairline ghost. Same
                // PillSurface primitive + `elevated` toggle as the surface
                // switcher and the agent bubbles, so the dialog buttons carry
                // the same clay language. Declared first (background) so the
                // label + MouseArea paint on top.
                PillSurface {
                    anchors.fill: parent
                    radius: Theme.radius.sm
                    elevated: button.highlighted
                    color: button.highlighted ? Theme.color.bg.selected : "transparent"
                    borderColor: button.highlighted ? button.modelData.accent : Theme.color.border.hairline
                }

                Text {
                    id: buttonLabel
                    anchors.centerIn: parent
                    text: button.modelData.label
                    // Highlighted: glow with the button's accent. Otherwise
                    // the normal chrome foreground.
                    color: button.highlighted
                           ? button.modelData.accent
                           : Theme.color.text.normal
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.xs
                    font.weight: button.highlighted
                                 ? Theme.font.weight.bold
                                 : Theme.font.weight.medium
                    renderType: Text.NativeRendering
                }

                // Mouse is an optional convenience — hovering pre-selects
                // the button (so Enter then confirms it) and a click
                // activates it directly. Keyboard alone remains sufficient.
                MouseArea {
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onEntered: root._highlight = button.modelData.idx
                    onClicked: {
                        root._highlight = button.modelData.idx;
                        root._activate();
                    }
                }
            }
        }
    }

    Text {
        anchors.horizontalCenter: parent.horizontalCenter
        text: "Enter → confirm · ←/→ choose · Esc → cancel"
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.xs
        renderType: Text.NativeRendering
    }
}
