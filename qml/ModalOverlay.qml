// Shared modal-overlay shell — the scrim + centered panel + FM scale-pop
// entrance + keyboard-modal focus-self-heal that every IDE modal needs.
//
// Used as the ROOT element of the concrete modals (AgentSpawnMenu,
// AgentSessionPicker, ConfirmDialog) — i.e. each of those IS-A
// ModalOverlay (QML root-replacement "inheritance"), inheriting this
// tree and these functions and contributing only its own content Column
// rows + key handling. This is the single home for three pieces of
// hard-won behaviour that used to be copy-pasted per modal:
//
//   1. The FM-style scale-pop entrance (states/transitions below) —
//      stabilised across rapid open→dismiss→reopen (see the long note on
//      the `states` block for WHY it is a to-only transition).
//   2. The keyboard-modal focus contract — keyCatcher holds active focus
//      so chords never leak to the surface underneath, and re-grabs focus
//      on the next tick if anything steals it while still visible.
//   3. Universal Esc→dismiss; every other key is forwarded (already
//      accepted, so the modal swallows it) via the `keyPressed` signal.
//
// Consumers override `open()` for per-modal reset (harness, fetch, …) and
// call the inherited `_show()` primitive to actually raise + focus. They
// drop their body straight into the default `content` slot, which is a
// Column with the standard panel margins + spacing already applied.
//
// All colour / typography / motion values bind against `Theme`.

import QtQuick

import "design"

Item {
    id: overlayRoot

    anchors.fill: parent
    visible: false
    z: 40 // above the which-key overlay (z 20) and the focus hairline

    // Panel width is the one geometry knob that differs per modal (the
    // spawn menu is 320, the session picker 420, …); height is always
    // driven by the content Column.
    property int panelWidth: 320

    // Consumer body goes here — a Column with panel margins + sm spacing
    // already applied, matching what every modal used to hand-roll.
    default property alias content: contentColumn.data

    signal dismissed()
    // Every key except Esc is forwarded here ALREADY accepted (the modal
    // swallows everything). Consumers switch on event.key for their own
    // actions; unhandled keys are simply absorbed.
    signal keyPressed(var event)

    // Raise + grab modal focus. The reusable primitive consumers call from
    // their own open()/reassert() after any per-modal state reset. Named
    // with a leading underscore so a consumer can shadow `open()` without
    // shadowing this.
    function _show() {
        overlayRoot.visible = true;
        keyCatcher.forceActiveFocus();
    }

    // Default open — fresh raise with no per-modal reset. Consumers that
    // need to reset state (harness, session list, …) override this and
    // call _show() at the end.
    function open() {
        _show();
    }

    // Idempotent focus re-assert for Main.qml's modal guard in
    // _restoreCentralFocus (window re-activation after Alt-Tab must not
    // leave an open modal visible-but-deaf). Distinct from open() because
    // open() may re-run a per-modal reset (re-fetch a list, reset harness)
    // that a re-assert must NOT trigger.
    function reassert() {
        _show();
    }

    function dismiss() {
        overlayRoot.visible = false;
        overlayRoot.dismissed();
    }

    // What Esc DOES — the single seam a staged modal overrides.
    //
    // Deliberately separate from dismiss(), which must keep meaning "close,
    // now" for its non-Esc callers: Main.qml calls agentSpawnMenu.dismiss()
    // when Ctrl+1..5 navigates away and from _dismissModals. Overloading
    // dismiss() with go-back semantics would make Ctrl+1..5 step back a
    // stage instead of navigating — exactly the class of regression the
    // Ctrl+1..5 modal exemption exists to prevent.
    //
    // Consumers cannot implement this in onKeyPressed: the keyCatcher below
    // handles Esc and RETURNS before keyPressed is emitted, so an Esc never
    // reaches a consumer's handler at all.
    function handleEscape() {
        dismiss();
    }

    // Entrance motion (FM-style scale-pop + opacity fade) is expressed as a
    // single root "shown" state + a to-only Transition, NOT as Behaviors on
    // visible-bound properties. A `to: "shown"` transition animates only the
    // way IN — leaving the state (dismiss) has no transition, so panel/scrim
    // snap straight to their hidden values. That (a) makes a rapid
    // open→dismiss→reopen always start the next pop from the full
    // popFromScale (no half-scaled, overshoot-less entrance), and (b) never
    // runs an exit animation against the already-hidden root Item. Only the
    // pop-IN is animated. (Behaviors with `enabled: visible` can't do this
    // safely — the enabled binding races the scale binding on open.)
    states: State {
        name: "shown"
        when: overlayRoot.visible
        PropertyChanges { target: panel; scale: 1; opacity: 1 }
        PropertyChanges { target: scrim; opacity: 1 }
    }
    transitions: Transition {
        to: "shown"
        NumberAnimation {
            target: panel
            property: "scale"
            duration: Theme.anim.duration
            easing.type: Easing.OutBack
            easing.overshoot: Theme.anim.popOvershoot
        }
        NumberAnimation {
            targets: [panel, scrim]
            property: "opacity"
            duration: Theme.anim.duration
            easing.type: Easing.BezierSpline
            easing.bezierCurve: Theme.anim.standardCurve
        }
    }

    // Dim the surface behind the panel so the modal state is legible.
    // Hidden-state default; the "shown" state fades it in with the panel.
    Rectangle {
        id: scrim
        anchors.fill: parent
        color: Theme.color.bg.scrim
        opacity: 0
    }

    // Framed panel — raised fill + hairline border, via the shared PillCard
    // primitive. fill/border/radius come from PillCard's defaults
    // (bg.RAISED / hairline / radius.lg), sourced from the one place so a
    // toolkit-wide retune carries through here for free.
    //
    // This used to describe a claymorphism panel whose depth (two shadows +
    // rim highlight + a bottom inner-shadow) is what made it read as floating.
    // The flat-aesthetic move zeroed that depth, so the RAISED FILL is now the
    // only thing separating this panel from the surface behind it — which is
    // why PillCard pins `bg.raised` rather than inheriting `bg.chrome`, and
    // why this comment names the token explicitly.
    PillCard {
        id: panel
        anchors.centerIn: parent
        width: overlayRoot.panelWidth
        implicitHeight: contentColumn.implicitHeight + Theme.spacing.lg * 2
        height: implicitHeight

        // Hidden-state defaults — the root "shown" state animates these to
        // scale 1 / opacity 1 on open via the FM-style transition above.
        // transformOrigin Center so the pop grows from the panel's middle
        // (it's anchored centerIn parent).
        transformOrigin: Item.Center
        scale: Theme.anim.popFromScale
        opacity: 0

        // Standard content column — panel margins + sm spacing, the layout
        // every modal used to declare inline. Anchored top/left/right (NOT
        // bottom) so height flows UP into panel.implicitHeight without a
        // binding loop (width flows down via the left/right anchors).
        Column {
            id: contentColumn
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: Theme.spacing.lg
            spacing: Theme.spacing.sm
        }
    }

    Item {
        id: keyCatcher
        // Modal key routing: while the overlay is visible this item holds
        // active focus, so chords never leak to the surface underneath.
        //
        // Self-heal: if anything steals active focus while the overlay is
        // still visible (window re-activation dispatch, a terminal pane
        // re-grabbing focus after Alt-Tab), take it back on the next
        // event-loop tick. Without this the modal goes deaf — visible but
        // receiving no keys, with no way to dismiss it. Every close path
        // flips visible to false BEFORE focus moves on, so this never
        // fights a legitimate focus handoff.
        onActiveFocusChanged: {
            if (!activeFocus && overlayRoot.visible)
                Qt.callLater(() => {
                    if (overlayRoot.visible)
                        keyCatcher.forceActiveFocus();
                });
        }
        Keys.onPressed: function (event) {
            // Modal: swallow everything. Esc routes through handleEscape()
            // (dismiss by default, overridable for staged modals); the rest
            // is the consumer's to interpret.
            event.accepted = true;
            if (event.key === Qt.Key_Escape) {
                overlayRoot.handleEscape();
                return;
            }
            overlayRoot.keyPressed(event);
        }
    }
}
