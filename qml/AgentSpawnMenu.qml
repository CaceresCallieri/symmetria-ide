// Keyboard-first agent menu (Ctrl+Shift+A — A-for-agent namespace).
//
// A centered modal panel. A single-letter key spawns a NEW / CONTINUE /
// RESUME session in the current HARNESS (the agent CLI — Claude or
// OpenCode). `o` toggles the harness, surfaced by the harness logo beside
// the title: the full Anthropic sunburst (Claude) or the OpenCode 3×3 grid,
// loaded as flat brand SVGs from the IDE's own asset tree (qml/assets/) and
// swapped live when `o` is pressed. The menu always re-opens on Claude, the
// daily-driver default.
//
// Permission polarity: spawns are DANGEROUS (skip-permissions) by default —
// the daily-driver polarity inherited from orchestrator.nvim's <leader>an
// family. One letter = go. Shift+letter still spawns the permission-checked
// variant, but that's a quiet power-user escape hatch and is intentionally
// NOT advertised in the menu anymore (the case distinction used to read as
// "o / O", "n / N", … — collapsed to a single letter per the simplify
// pass). Esc dismisses. No mouse interaction — chord-driven per the
// keyboard-first non-negotiable.
//
// Resume semantics differ per harness: claude's bare `-r` opens its own
// interactive picker inside the terminal; opencode's `--session` requires
// an id, so `r` on the OpenCode harness defers to the AgentSessionPicker
// overlay (wired in Main.qml via resumePickerRequested).
//
// The scrim + centered panel + FM scale-pop entrance + keyboard-modal
// focus-self-heal all come from ModalOverlay (this is the root element);
// see that file for the entrance / focus contract. All color and
// typography values bind against the `Theme` singleton.

import QtQuick

import "design"

ModalOverlay {
    id: root

    panelWidth: 320

    // The agent CLI the n/c/r keys spawn. Always resets to claude on
    // open() — harness choice is per-spawn, not sticky session state.
    property string harness: "claude"

    // Header brand-logo edge length. Sized just above the title text
    // (Theme.font.size.sm == 10) so the full Anthropic / OpenCode glyph
    // reads as a header badge without overpowering the title — 22 px was
    // too loud, 15 still a touch big, 13 sits right next to the label.
    readonly property int _headerIconSize: 13

    // OpenCode resume needs a session id — Main.qml routes this to the
    // AgentSessionPicker overlay (carries the dangerous polarity the
    // user chose with the case of the `r` keypress).
    signal resumePickerRequested(bool dangerous)

    // Override the base open() to reset the harness to the daily-driver
    // default before raising. reassert() (used by Main.qml's modal guard
    // on window re-activation) is INHERITED unchanged precisely because it
    // must NOT reset harness — calling open() there would clobber an
    // OpenCode selection back to Claude every time the user Alt-Tabbed out.
    function open() {
        root.harness = "claude";
        _show();
    }

    function _spawn(spawnType, dangerous) {
        // spawn_agent appends in display order, focuses the new agent
        // and switches the central surface, which lands keyboard focus
        // on the agent terminal — no explicit focus restore needed.
        root.visible = false;
        controller.spawn_agent(spawnType, dangerous, root.harness);
    }

    function _resume(dangerous) {
        if (root.harness === "opencode") {
            // No picker flag in the opencode CLI — hand off to the IDE's
            // session picker, which spawns `--session <id>` on accept.
            root.visible = false;
            root.resumePickerRequested(dangerous);
            return;
        }
        root._spawn("resume", dangerous);
    }

    // Esc is handled by ModalOverlay (→ dismiss); everything else lands
    // here already accepted (the modal swallows it).
    onKeyPressed: function (event) {
        switch (event.key) {
        case Qt.Key_O:
            root.harness = root.harness === "claude" ? "opencode" : "claude";
            break;
        case Qt.Key_N:
            root._spawn("fresh", !(event.modifiers & Qt.ShiftModifier));
            break;
        case Qt.Key_C:
            root._spawn("continue", !(event.modifiers & Qt.ShiftModifier));
            break;
        case Qt.Key_R:
            root._resume(!(event.modifiers & Qt.ShiftModifier));
            break;
        default:
            // Swallow everything else — modal.
            break;
        }
    }

    // ---- Panel content (dropped into ModalOverlay's content Column) ----

    // Header — harness logo + title, centered as a unit. The logo is the
    // full brand glyph for the current harness: the Anthropic sunburst
    // (Claude) or the OpenCode 3×3 grid. Source swaps on `root.harness`,
    // so pressing `o` flips the logo in place. No arrow — the title is a
    // plain "Spawn agent #N" label (numbering is dense display order, so a
    // new agent always becomes #(count + 1)).
    Row {
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: Theme.spacing.sm

        Image {
            anchors.verticalCenter: parent.verticalCenter
            // Full brand logo, copied into the IDE's own asset tree
            // from ~/.dotfiles/scripts/{claude,opencode}-icon.svg
            // (the glyphs the desktop notification stack uses) so
            // the repo carries its own copy — no runtime dependency
            // on the user's dotfiles. Brand fills are baked into the
            // SVGs (Claude #D97757 / OpenCode #6f9bd6) and identify
            // the backend regardless of theme — intentionally NOT
            // Theme-tokened. Path is relative to this QML file.
            source: root.harness === "opencode"
                    ? "assets/opencode-icon.svg"
                    : "assets/claude-icon.svg"
            // Rasterize the vector at 2× the display box so the
            // glyph stays crisp at this small size.
            sourceSize.width: root._headerIconSize * 2
            sourceSize.height: root._headerIconSize * 2
            width: root._headerIconSize
            height: root._headerIconSize
            smooth: true
            fillMode: Image.PreserveAspectFit
        }

        Text {
            anchors.verticalCenter: parent.verticalCenter
            text: "Spawn agent #" + (controller.agentOrder.length + 1)
            color: Theme.color.text.strong
            font.family: Theme.font.family
            font.pixelSize: Theme.font.size.sm
            font.weight: Theme.font.weight.bold
            renderType: Text.NativeRendering
        }
    }

    // Extra breathing room below the centered header, setting it apart
    // from the left-aligned key rows — the Column's uniform sm gap alone
    // read too tight at this seam.
    Item { width: 1; height: Theme.spacing.sm }

    // Action list — one single-letter key per row. Keys are single
    // monospace glyphs, so the label column self-aligns without a fixed-
    // width key cell. n/c/r spawn in the current harness; o toggles the
    // harness (its only behaviour — reflected by the header icon above).
    Repeater {
        model: [
            { key: "n", label: "new session" },
            { key: "c", label: "continue" },
            { key: "r", label: root.harness === "opencode"
                               ? "resume (session picker)"
                               : "resume (claude's picker)" },
            { key: "o", label: "switch harness" },
        ]

        delegate: Row {
            id: entryRow
            required property var modelData
            spacing: Theme.spacing.sm

            Text {
                text: entryRow.modelData.key
                color: Theme.color.text.strong
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.xs
                font.weight: Theme.font.weight.medium
                renderType: Text.NativeRendering
            }
            Text {
                text: entryRow.modelData.label
                color: Theme.color.text.normal
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.xs
                renderType: Text.NativeRendering
            }
        }
    }

    // Matching breathing room above the footer, separating the action
    // list from the Esc hint (same intent as the spacer above the list).
    Item { width: 1; height: Theme.spacing.sm }

    // The one surviving footer hint — the skip-permissions / safe-mode
    // explanation lines were removed in the simplify pass (dangerous is
    // now the unadvertised default).
    Text {
        anchors.horizontalCenter: parent.horizontalCenter
        text: "Esc → cancel"
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.xs
        renderType: Text.NativeRendering
    }
}
