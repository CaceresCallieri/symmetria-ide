// Subscription-usage detail — the "everything the bar omits" surface.
//
// Opened by hovering `UsageIndicator` or by Ctrl+Shift+I from any surface, and
// mounted at WINDOW scope (Main.qml) rather than inside the status bar, because
// a panel parented into a ~24px strip would be clipped by it.
//
// ⚠ Deliberately NOT a `ModalOverlay`, unlike every other IDE popup. This one
// is read-only information that also opens on hover; a scrim plus the modal
// focus-grab would darken the whole IDE and swallow keys every time the pointer
// crossed the corner. It therefore reimplements only the two things it does
// need — the FM scale-pop entrance and Esc-to-dismiss — and leaves focus alone.
// Do NOT "fix" this to inherit ModalOverlay: hover-opening a modal is the bug
// that change would introduce.
//
// The entrance mirrors ModalOverlay's to-only Transition for the same reason
// documented there: a rapid open→close→open must always start from the full
// popFromScale rather than resuming a half-finished pop.

// Required because the nested Repeater delegates below reference ids from this
// file's scope (`root.nowMs`, `providerBlock.width`). Without it those reads
// are "unqualified" — they still WORK, but the delegate resolves them through a
// dynamic context lookup at evaluation time instead of binding them when the
// delegate is created, which is both slower and what qmllint flags. This is the
// documented fix, not a lint suppression.
pragma ComponentBehavior: Bound

import QtQuick

import "design"

Item {
    id: root

    // AppController.usageProviders. Guard with `!= null && .length`, never
    // Array.isArray (qml_qvariantlist_array_check memory).
    property var providers: null

    // True while the pointer is over the card — Main.qml ORs this with the
    // indicator's own hover so moving the pointer from one to the other does
    // not close the popup mid-travel.
    readonly property alias hovered: cardHover.hovered

    property double nowMs: Date.now()

    implicitWidth: card.width
    implicitHeight: card.height
    visible: false

    function open() {
        root.nowMs = Date.now();
        root.visible = true;
    }

    function close() {
        root.visible = false;
    }

    Timer {
        interval: 30000
        repeat: true
        running: root.visible
        triggeredOnStart: true
        onTriggered: root.nowMs = Date.now()
    }

    // ⚠ Esc is deliberately NOT handled, and this Item never takes focus.
    // Esc is load-bearing in the agent panes — it is how the user interrupts a
    // running Claude turn — so an informational popup that swallowed it would
    // cost the user a cancel at exactly the wrong moment. Closing is therefore:
    // move the pointer away (hover case), or press the chord again (pinned
    // case). Both live in Main.qml.

    states: State {
        name: "shown"
        when: root.visible
        // Qt 6 grouped-target form (`card.scale:`) rather than the
        // `target:` + bare-property form ModalOverlay still uses: the older
        // spelling is custom-parsed and qmllint flags all three lines. Same
        // semantics, and new files are required to arrive lint-clean.
        PropertyChanges {
            card.scale: 1
            card.opacity: 1
        }
    }
    transitions: Transition {
        to: "shown"
        NumberAnimation {
            target: card
            property: "scale"
            duration: Theme.anim.duration
            easing.type: Easing.OutBack
            easing.overshoot: Theme.anim.popOvershoot
        }
        NumberAnimation {
            target: card
            property: "opacity"
            duration: Theme.anim.duration
            easing.type: Easing.BezierSpline
            easing.bezierCurve: Theme.anim.standardCurve
        }
    }

    PillCard {
        id: card

        width: 300
        implicitHeight: body.implicitHeight + Theme.spacing.lg * 2
        height: implicitHeight

        // Grows from the bottom-right, the corner it is anchored to, so the
        // pop reads as "unfolding out of the indicator" rather than drifting.
        transformOrigin: Item.BottomRight
        scale: Theme.anim.popFromScale
        opacity: 0

        HoverHandler {
            id: cardHover
        }

        Column {
            id: body
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: Theme.spacing.lg
            spacing: Theme.spacing.md

            Repeater {
                model: root.providers

                Column {
                    id: providerBlock

                    required property var modelData

                    // `body` is already inset by its own anchors.margins, so
                    // this is its full width — subtracting the margin again
                    // here would inset every block a second time.
                    width: body.width
                    spacing: Theme.spacing.xs

                    // Header: glyph + name + plan, with the observation age on
                    // the right. The age is the honesty valve — it is how the
                    // user tells "3%" from "3% as of four hours ago".
                    Item {
                        width: parent.width
                        height: providerName.implicitHeight

                        Row {
                            anchors.left: parent.left
                            anchors.verticalCenter: parent.verticalCenter
                            spacing: Theme.spacing.xs

                            // Same brand mark as the compact readout, same
                            // size — the popup is the readout expanded, not a
                            // different surface with its own iconography.
                            Image {
                                anchors.verticalCenter: parent.verticalCenter
                                source: UsageFormat.providerIcon(providerBlock.modelData.provider)
                                sourceSize.width: Theme.size.usageProviderIcon * 2
                                sourceSize.height: Theme.size.usageProviderIcon * 2
                                width: Theme.size.usageProviderIcon
                                height: Theme.size.usageProviderIcon
                                smooth: true
                                fillMode: Image.PreserveAspectFit
                            }
                            Text {
                                id: providerName
                                text: providerBlock.modelData.label
                                color: Theme.color.text.strong
                                font.family: Theme.font.family
                                font.pixelSize: Theme.font.size.sm
                                font.weight: Theme.font.weight.bold
                                renderType: Text.NativeRendering
                            }
                            Text {
                                visible: (providerBlock.modelData.plan || "") !== ""
                                text: providerBlock.modelData.plan
                                color: Theme.color.text.dim
                                font.family: Theme.font.family
                                font.pixelSize: Theme.font.size.xs
                                renderType: Text.NativeRendering
                            }
                        }

                        Text {
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            text: UsageFormat.age(providerBlock.modelData.observedAt, root.nowMs)
                            color: Theme.color.text.dim
                            font.family: Theme.font.family
                            font.pixelSize: Theme.font.size.xs
                            renderType: Text.NativeRendering
                        }
                    }

                    // One row per window: name, percentage, a proportional bar,
                    // and the real countdown to its reset — the "how long until
                    // the next window" the compact bar has no room for.
                    Repeater {
                        model: providerBlock.modelData.windows

                        Column {
                            id: windowRow

                            required property var modelData

                            width: providerBlock.width
                            spacing: Theme.spacing.xxs

                            Item {
                                width: parent.width
                                height: windowLabel.implicitHeight

                                Row {
                                    anchors.left: parent.left
                                    spacing: Theme.spacing.xs

                                    Text {
                                        id: windowLabel
                                        text: windowRow.modelData.label
                                        color: Theme.color.text.normal
                                        font.family: Theme.font.family
                                        font.pixelSize: Theme.font.size.xs
                                        renderType: Text.NativeRendering
                                    }
                                    // Per-model buckets carry the model name;
                                    // account-wide windows carry "".
                                    Text {
                                        visible: (windowRow.modelData.scope || "") !== ""
                                        text: windowRow.modelData.scope
                                        color: Theme.color.text.dim
                                        font.family: Theme.font.family
                                        font.pixelSize: Theme.font.size.xs
                                        renderType: Text.NativeRendering
                                    }
                                }

                                Row {
                                    anchors.right: parent.right
                                    spacing: Theme.spacing.xs

                                    Text {
                                        readonly property string cd: UsageFormat.countdown(
                                            windowRow.modelData.resets_at, root.nowMs)
                                        visible: cd !== ""
                                        text: "⟲" + cd
                                        color: Theme.color.text.dim
                                        font.family: Theme.font.family
                                        font.pixelSize: Theme.font.size.xs
                                        renderType: Text.NativeRendering
                                    }
                                    Text {
                                        text: (windowRow.modelData.pct || 0) + "%"
                                        color: UsageFormat.usageColor(windowRow.modelData.pct || 0)
                                        font.family: Theme.font.family
                                        font.pixelSize: Theme.font.size.xs
                                        renderType: Text.NativeRendering
                                    }
                                }
                            }

                            // Proportional bar — the percentage again, as a
                            // shape, so the popup is scannable without reading
                            // every number.
                            Rectangle {
                                width: parent.width
                                // Hairline bar: 2px track, 1px radius. Literal
                                // rather than a Theme token because this is the
                                // only bar in the IDE — promote it the moment a
                                // second surface needs one.
                                height: 2
                                radius: 1
                                color: Theme.color.border.hairline

                                Rectangle {
                                    width: parent.width * Math.min(
                                        1, Math.max(0, (windowRow.modelData.pct || 0) / 100))
                                    height: parent.height
                                    radius: parent.radius
                                    color: UsageFormat.usageColor(windowRow.modelData.pct || 0)
                                }
                            }
                        }
                    }

                    // Extra-usage credits, when the provider reports any.
                    Text {
                        readonly property var credits: providerBlock.modelData.extra
                            ? providerBlock.modelData.extra.credits
                            : null
                        // Only when there is something to say. Codex reports a
                        // credits block even on a plan with none, and a bare
                        // "credits 0" is noise dressed as information.
                        visible: credits != null
                                 && (credits.limit !== undefined
                                     || credits.unlimited
                                     || credits.has_credits)
                        text: {
                            if (credits == null)
                                return "";
                            if (credits.limit !== undefined)
                                return "credits " + credits.used + "/" + credits.limit;
                            if (credits.unlimited)
                                return "credits unlimited";
                            return "credits " + (credits.balance || "0");
                        }
                        color: Theme.color.text.dim
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        renderType: Text.NativeRendering
                    }

                    Text {
                        readonly property int resetCredits: providerBlock.modelData.extra
                            ? (providerBlock.modelData.extra.reset_credits || 0)
                            : 0
                        visible: resetCredits > 0
                        text: resetCredits + " limit reset available"
                        color: Theme.color.accent.primary
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        renderType: Text.NativeRendering
                    }

                    // The live poll failure, in words. Present only while the
                    // provider is actually failing; the numbers above are the
                    // last good ones (an errored snapshot never overwrites
                    // them — see usage_providers' error contract).
                    Text {
                        visible: (providerBlock.modelData.error || "") !== ""
                        text: "not updating: " + UsageFormat.errorText(providerBlock.modelData.error)
                        color: Theme.color.usage.crit
                        font.family: Theme.font.family
                        font.pixelSize: Theme.font.size.xs
                        renderType: Text.NativeRendering
                    }
                }
            }
        }
    }
}
