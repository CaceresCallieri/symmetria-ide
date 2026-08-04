// Compact subscription-usage readout — the always-visible half of the usage
// panel. Lives in StatusBar's right-hand column (the strip under the side
// panel), which was deliberately empty until now.
//
// Shows the SMALLEST honest thing: one row per provider, each a glyph plus the
// account-wide windows as "<label> <pct>%". Everything else the providers
// report — per-model buckets, credits, plan, exact reset times, observation age
// — belongs to `UsageDetailPopup`, reached by hover or Ctrl+Shift+I. Keeping
// the bar minimal is the point: it is a glance, not a dashboard.
//
// Data comes from `controller.usageProviders` (see AppController.usageProviders
// for the row shape). A provider whose session window does not exist — Codex
// today exposes only a weekly one — simply renders one number instead of two,
// because `session` arrives as an empty object rather than a placeholder.

import QtQuick

import "design"

Item {
    id: root

    // AppController.usageProviders — a QVariantList of provider rows. Guard
    // with `!= null && .length` and NEVER Array.isArray: PySide6 list
    // properties fail that check in Qt 6.11 (see the qml_qvariantlist_array_check
    // memory).
    property var providers: null

    // Live clock for the reset countdowns / observation age. Ticked by the
    // Timer below only while this is visible, so an unseen status bar costs
    // nothing. Seeded with Date.now() rather than 0 so the first paint is sane.
    property double nowMs: Date.now()

    // True while the pointer is over the readout — Main.qml opens the detail
    // popup from this (and keeps it open while the pointer is over the popup).
    readonly property bool hovered: hoverArea.containsMouse

    // Bound to controller.usageRefreshing: a user-requested poll is running.
    property bool refreshing: false

    // The user clicked the readout asking for fresh numbers. Emitted rather
    // than calling the controller here, so this component stays free of the
    // context property and can be rendered by a harness (or reused) as-is.
    signal refreshRequested

    readonly property bool hasRows: providers != null && providers.length > 0

    implicitWidth: layout.implicitWidth
    implicitHeight: layout.implicitHeight
    visible: hasRows

    // Pulse while refreshing. The animation drives `_pulse` — an ordinary
    // property with no binding — and `opacity` merely READS it, because a
    // `NumberAnimation on opacity` would destroy opacity's binding for good
    // and leave the readout stuck at whatever value the last frame wrote.
    property real _pulse: 1.0
    opacity: root.refreshing ? root._pulse : 1.0

    SequentialAnimation {
        running: root.refreshing
        loops: Animation.Infinite
        NumberAnimation {
            target: root
            property: "_pulse"
            to: 0.35
            duration: Theme.anim.duration
            easing.type: Easing.InOutQuad
        }
        NumberAnimation {
            target: root
            property: "_pulse"
            to: 1.0
            duration: Theme.anim.duration
            easing.type: Easing.InOutQuad
        }
    }

    Timer {
        // 30s is minute-honest for a countdown rendered in whole minutes, at a
        // negligible cost. Same cadence (and reasoning) as StatusBar's clock.
        interval: 30000
        repeat: true
        running: root.visible
        triggeredOnStart: true
        onTriggered: root.nowMs = Date.now()
    }

    Row {
        id: layout
        anchors.verticalCenter: parent.verticalCenter
        anchors.right: parent.right
        spacing: Theme.spacing.md

        Repeater {
            model: root.providers

            Row {
                id: providerRow

                required property var modelData

                spacing: Theme.spacing.xs

                // The provider's real brand mark. Brand fills are baked into
                // the SVGs and identify the account regardless of theme —
                // intentionally NOT Theme-tokened, the same call the spawn
                // menu's icons make.
                Image {
                    anchors.verticalCenter: parent.verticalCenter
                    source: UsageFormat.providerIcon(providerRow.modelData.provider)
                    // Rasterize at 2x the display box so the mark stays crisp
                    // at this size (the spawn menu's icons do the same).
                    sourceSize.width: Theme.size.usageProviderIcon * 2
                    sourceSize.height: Theme.size.usageProviderIcon * 2
                    width: Theme.size.usageProviderIcon
                    height: Theme.size.usageProviderIcon
                    smooth: true
                    fillMode: Image.PreserveAspectFit
                }

                // Session (5h) — absent for providers that expose no such
                // window, in which case `session` is {} and this collapses.
                UsageReading {
                    window: providerRow.modelData.session
                }

                UsageReading {
                    window: providerRow.modelData.weekly
                }

                // A failing poll must be visible without stealing the row: the
                // last known numbers stay, and this marks them as no longer
                // being refreshed. The reason itself is in the popup.
                Text {
                    visible: (providerRow.modelData.error || "") !== ""
                    text: "!"
                    color: Theme.color.usage.crit
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    renderType: Text.NativeRendering
                }
            }
        }
    }

    // One "<label> <pct>%" reading. Inline component so the session and weekly
    // slots share a definition instead of being a copy-paste pair.
    component UsageReading: Row {
        id: reading

        // A window dict from the provider row, or {} when that window does not
        // exist for this provider.
        property var window: null

        visible: window != null && (window.label || "") !== ""
        spacing: Theme.spacing.xxs

        Text {
            // `window` is a non-null but EMPTY object for a provider with no
            // such window (Codex has no session one), so the truthiness of
            // `window` alone is not enough — `.label` is undefined there, and
            // QML warns on assigning undefined to a string property.
            text: (reading.window && reading.window.label) || ""
            color: Theme.color.text.dim
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.xs
            renderType: Text.NativeRendering
        }
        Text {
            readonly property int pct: reading.window ? (reading.window.pct || 0) : 0
            text: pct + "%"
            color: UsageFormat.usageColor(pct)
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.sm
            renderType: Text.NativeRendering
        }
    }

    MouseArea {
        id: hoverArea
        anchors.fill: parent
        // `hoverEnabled` genuinely exists on MouseArea — this is the one type
        // family that has it (see the qml_property_must_exist_on_type rule).
        hoverEnabled: true
        // Left-click forces a poll of every provider. Only the left button is
        // taken; right/middle still fall through to whatever sits behind the
        // status bar. A click during a refresh is ignored by the poller, which
        // adopts the running round rather than queueing a second one.
        acceptedButtons: Qt.LeftButton
        cursorShape: Qt.PointingHandCursor
        onClicked: root.refreshRequested()
    }
}
