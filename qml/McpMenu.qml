// Per-project MCP toggles (Ctrl+Shift+M — M-for-MCP). A keyboard-first
// modal: one single-letter key per MCP server flips whether THIS project's
// spawned agents get it. Default OFF; the choice persists in the committable
// `.symmetria/ide.json` marker (project_browser_marker / AppController), so
// it travels with the repo. The panel stays open after a toggle so the state
// flips in place — Esc dismisses.
//
// Today one entry — chrome devtools (`w`, the embedded-browser capability);
// the design anticipates "a couple of MCPs this way", so adding another is a
// three-liner: a row below + a case in onKeyPressed + the controller
// property/slot it binds. Surfacing these toggles HERE (vs a persistent
// top-bar affordance) keeps the AgentTopBar clean.
//
// Mirrors AgentSpawnMenu: ModalOverlay root (scrim + centered clay panel +
// FM scale-pop entrance + keyboard-modal focus self-heal), Theme tokens
// only, no mouse-required interaction. See ModalOverlay for the entrance /
// focus contract.

import QtQuick

import "design"

ModalOverlay {
    id: root

    panelWidth: 320

    // Esc is handled by ModalOverlay (→ dismiss); everything else lands here
    // already accepted (the modal swallows it). The panel stays open after a
    // toggle so the state flips in place — toggle again or Esc.
    onKeyPressed: function (event) {
        switch (event.key) {
        case Qt.Key_W:
            // Google Chrome DevTools MCP (the embedded-browser capability).
            controller.toggle_project_browser();
            break;
        default:
            break; // modal — swallow everything else
        }
    }

    // ---- Panel content (dropped into ModalOverlay's content Column) ----

    Text {
        anchors.horizontalCenter: parent.horizontalCenter
        text: "Project MCPs"
        color: Theme.color.text.strong
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.sm
        font.weight: Theme.font.weight.bold
        renderType: Text.NativeRendering
    }

    Text {
        anchors.horizontalCenter: parent.horizontalCenter
        text: "for this project's agents"
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.xs
        renderType: Text.NativeRendering
    }

    // Breathing room below the centered header, before the left-aligned rows
    // (mirrors AgentSpawnMenu's header/list seam).
    Item { width: 1; height: Theme.spacing.sm }

    // One row per toggleable MCP — `key  name  state`. `state` binds LIVE to
    // the controller so it flips the instant the key toggles it (a static
    // value would read once — gotcha #3). Single monospace key glyph, so the
    // label column self-aligns like AgentSpawnMenu's action list.
    Row {
        spacing: Theme.spacing.sm

        Text {
            anchors.verticalCenter: parent.verticalCenter
            text: "w"
            color: Theme.color.text.strong
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.xs
            font.weight: Theme.font.weight.medium
            renderType: Text.NativeRendering
        }
        Text {
            anchors.verticalCenter: parent.verticalCenter
            text: "chrome devtools"
            color: Theme.color.text.normal
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.xs
            renderType: Text.NativeRendering
        }
        Text {
            anchors.verticalCenter: parent.verticalCenter
            text: controller.projectBrowserEnabled ? "on" : "off"
            color: controller.projectBrowserEnabled
                ? Theme.color.accent.primary
                : Theme.color.text.dim
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.xs
            font.weight: controller.projectBrowserEnabled
                ? Theme.font.weight.bold
                : Theme.font.weight.medium
            renderType: Text.NativeRendering
        }
    }

    // Matching breathing room above the footer (same intent as the spacer
    // above the list).
    Item { width: 1; height: Theme.spacing.sm }

    Text {
        anchors.horizontalCenter: parent.horizontalCenter
        text: "Esc → close"
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.xs
        renderType: Text.NativeRendering
    }
}
