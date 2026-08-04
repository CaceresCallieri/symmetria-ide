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

    readonly property bool hasRows: providers != null && providers.length > 0

    implicitWidth: layout.implicitWidth
    implicitHeight: layout.implicitHeight
    visible: hasRows

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

                Text {
                    text: providerRow.modelData.provider === "codex"
                        ? Theme.glyph.providerCodex
                        : Theme.glyph.providerClaude
                    color: Theme.color.text.dim
                    // Theme.font.family IS `editorFontFamily` (Theme.qml binds
                    // it directly), so the glyph resolves through the same
                    // Nerd Font cascade Theme.glyph's contract requires — while
                    // avoiding the unqualified context-property access the two
                    // older glyph call sites incur.
                    font.family: Theme.font.family
                    font.pixelSize: Theme.font.size.sm
                    renderType: Text.NativeRendering
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
        // Pass clicks through: the readout is informational, and the popup is
        // reached by hover or by chord. Accepting clicks here would swallow
        // them from whatever sits behind the status bar.
        acceptedButtons: Qt.NoButton
    }
}
