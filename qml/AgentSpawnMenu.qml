// Keyboard-first agent chooser (Ctrl+Shift+A — A-for-agent namespace).
//
// A centered modal panel in TWO STAGES: stage 0 picks the HARNESS (the
// agent CLI — p/c/o for Pi / Claude / OpenCode), stage 1 picks the ACTION
// (n/c/r — new / continue / resume) and spawns. The stages exist because a
// single flat menu cannot scale past two harnesses: the old `o` toggle
// cycled Claude⇄OpenCode with the choice legible only from the header
// logo, which stops working the moment there are three.
//
// Every stage-0 row — its key, its label, its brand mark, and its stage-1
// resume wording — is projected from `controller.agentHarnessCatalog`,
// which is itself projected from agent_harness.HARNESSES. There is no
// harness table in this file: adding a fourth harness is a registry entry
// plus an icon, and this menu grows a row for it with no QML edit.
//
// A harness whose executable is missing from PATH is shown DIMMED and
// labelled "(not installed)", rather than omitted — a silently shorter menu
// reads as "the IDE forgot about Pi". The row is not selectable, so its key
// does nothing when pressed; the label is what says why.
// `spawn_agent` remains the real PATH guard.
//
// Permission polarity: spawns are DANGEROUS (skip-permissions) by default —
// the daily-driver polarity inherited from orchestrator.nvim's <leader>an
// family. One letter = go. Shift+letter still spawns the permission-checked
// variant, but that's a quiet power-user escape hatch and is intentionally
// NOT advertised in the menu (the case distinction used to read as "n / N",
// … — collapsed to a single letter per the simplify pass). ⚠ "dangerous"
// does not mean the same thing for every harness — see the AgentHarness
// docstring; for Pi it only suppresses the project-trust dialog.
//
// Esc is stage-aware: at stage 1 it steps BACK to the harness chooser, at
// stage 0 it cancels. That is routed through ModalOverlay's `handleEscape`
// seam and NOT through `dismiss()`, which must keep meaning "close, now"
// for Main.qml's Ctrl+1..5 navigate-away path. No mouse interaction — chord
// -driven per the keyboard-first non-negotiable.
//
// Resume semantics differ per harness and are read from the catalog's
// `resumeRequiresId`, never from a harness-name test: claude and pi open
// their OWN interactive picker inside the pane, opencode's `--session`
// requires an id so `r` defers to the AgentSessionPicker overlay (wired in
// Main.qml via resumePickerRequested, which carries the harness).
//
// The scrim + centered panel + FM scale-pop entrance + keyboard-modal
// focus-self-heal all come from ModalOverlay (this is the root element);
// see that file for the entrance / focus contract. All color and
// typography values bind against the `Theme` singleton.

// `ComponentBehavior: Bound` so the row delegate below may reference this
// file's ids (`root`) — it is instantiated from here and nowhere else.
pragma ComponentBehavior: Bound

import QtQuick

import "design"

ModalOverlay {
    id: root

    panelWidth: 320

    // 0 = harness chooser, 1 = action chooser. Reset by open(), preserved
    // by the inherited reassert().
    property int stage: 0

    // The agent CLI stage 1 spawns. Empty at stage 0 — nothing chosen yet.
    // Per-spawn, not sticky session state: open() always clears it.
    property string harness: ""

    // Header brand-logo edge length. Sized just above the title text
    // (Theme.font.size.sm == 10) so the full brand glyph reads as a header
    // badge without overpowering the title — 22 px was too loud, 15 still a
    // touch big, 13 sits right next to the label.
    readonly property int _headerIconSize: 13

    // A harness whose executable is not on PATH is marked by COLOUR alone
    // (Theme.color.text.dim), never by an extra opacity reduction: dim on the
    // panel fill is already the toolkit's "inert" step, and multiplying it by
    // a further 0.45 took the row to ~1.8:1 contrast — unreadable, so the user
    // could no longer read WHICH harness is missing, which is the entire point
    // of keeping the row. Opacity is also inherited multiplicatively through
    // the panel's scale-pop, so it was never a stable signal anyway.

    // In the VPS location the menu spawns/attaches REMOTE sessions: remote
    // is claude-only in v1, so the harness stage is SKIPPED entirely (open()
    // lands straight on stage 1 with claude selected) and `a` appears,
    // opening the RemoteSessionPicker to attach an existing tmux session
    // (usually phone-started). n/c/r keep their meaning, running remotely
    // via ssh+tmux.
    readonly property bool vpsMode: controller.location === "vps"

    // The whole row list, for stage 0. Single-harness lookups go through
    // `controller.harness_menu_entry(name)` instead — the one scan shared with
    // AgentSessionPicker, rather than a copy of it per modal.
    readonly property var _catalog: controller.agentHarnessCatalog

    // Display number of the slot about to be filled (dense display order, so
    // a new agent is always #(count + 1)). Hoisted out of the header text so
    // both stage labels read the same value from one place.
    readonly property int _nextAgentNumber: controller.agentOrder.length + 1

    // This file's view model: the rows the Repeater below renders, for
    // whichever stage is current. Declared here with the rest of the menu
    // state rather than beside the Repeater, so the whole of what this modal
    // knows reads in one block.
    //
    // Every input is read in this EXPRESSION and passed down as an argument,
    // rather than reached for inside the row builders: that is what makes
    // `stage`, `_catalog`, `harness` and `vpsMode` registered dependencies, so
    // the list swaps on a stage change and re-dims when a harness appears on
    // PATH. Private because a public `rows` invited an outside writer to
    // replace the binding and silently freeze the menu on one stage.
    readonly property var _rows: root.stage === 0
        ? root._harnessRows(root._catalog)
        : root._actionRows(root.harness, root.vpsMode)

    // Thin delegation to the controller's shared lookup — the scan itself is
    // NOT duplicated here (AgentSessionPicker calls the same slot). This
    // exists only so the three per-harness decisions below name one function
    // instead of repeating the controller access.
    function _entry(name) {
        return controller.harness_menu_entry(name);
    }

    // Resume-by-id harnesses (opencode) need a session id — Main.qml routes
    // this to the AgentSessionPicker overlay. Carries the HARNESS as well as
    // the dangerous polarity the user chose with the case of the `r`
    // keypress, so the picker spawns the harness that raised it rather than
    // assuming opencode.
    signal resumePickerRequested(string harness, bool dangerous)

    // VPS attach — Main.qml routes this to the RemoteSessionPicker overlay
    // (same handoff shape as resumePickerRequested).
    signal attachPickerRequested(bool dangerous)

    // Override the base open() to reset the wizard before raising.
    // reassert() (used by Main.qml's modal guard on window re-activation) is
    // INHERITED unchanged precisely because it must NOT reset: calling
    // open() there would throw the user back to stage 0 every time they
    // Alt-Tabbed out mid-choice.
    function open() {
        // Re-read PATH first: this is the moment the row list is about to be
        // shown, and the controller only notifies when the answer moved, so a
        // harness installed since the last open lights up here and nowhere
        // else.
        controller.refresh_harness_availability();
        if (root.vpsMode) {
            // Remote is claude-only — there is no harness to choose.
            root.harness = "claude";
            root.stage = 1;
        } else {
            root.harness = "";
            root.stage = 0;
        }
        _show();
    }

    // The location toggle (Ctrl+Shift+U) is a live chord and can fire while
    // this menu is up. Local and vps offer DIFFERENT harness sets — vps is
    // claude-only in v1 — so a stale stage-0 selection would let the user pick
    // Pi and only learn it was refused after the menu had already closed.
    // Restarting the wizard is the honest response: the choice being made no
    // longer exists.
    onVpsModeChanged: {
        if (root.visible)
            root.open();
    }

    // Stage-aware Esc, via ModalOverlay's seam. In vps mode stage 1 IS the
    // first stage, so there is nothing to step back to and Esc cancels.
    function handleEscape() {
        if (root.stage === 1 && !root.vpsMode) {
            root.stage = 0;
            root.harness = "";
            return;
        }
        dismiss();
    }

    // Stage-0 rows. `menuKey` is uppercase in the registry precisely so it
    // can be compared against Qt's key enum below. The catalog is a PARAMETER
    // rather than a read of `root._catalog` inside the body, so the `_rows`
    // binding that calls this registers it as a dependency and re-evaluates
    // when availability moves.
    function _harnessRows(catalog) {
        var rows = [];
        for (var i = 0; i < catalog.length; ++i) {
            var entry = catalog[i];
            rows.push({
                key: entry.menuKey.toLowerCase(),
                // An unavailable row STATES its reason rather than only
                // looking different: dim colour alone is a signal the user has
                // to already know how to read, and the row's key silently does
                // nothing when pressed — "(not installed)" is what turns that
                // dead keypress from a bug into an answer.
                label: entry.available
                       ? entry.label
                       : entry.label + " (not installed)",
                icon: entry.icon,
                dim: !entry.available,
            });
        }
        return rows;
    }

    // Stage-1 rows. The `r` wording is the chosen harness's own, so the user
    // is told WHICH picker `r` is about to open before pressing it. `icon`
    // and `dim` are simply absent here — the delegate treats a missing
    // optional field as falsy, so spelling out `icon: "", dim: false` on every
    // action row added noise and no meaning.
    function _actionRows(harness, vps) {
        var entry = root._entry(harness);
        var rows = [
            { key: "n", label: "new session" },
            { key: "c", label: "continue" },
            { key: "r", label: entry ? entry.resumeLabel : "resume" },
        ];
        if (vps)
            rows.push({ key: "a", label: "attach tmux session" });
        return rows;
    }

    // Qt's letter key enum IS the uppercase ASCII code (Qt.Key_P == 0x50 ==
    // "P".charCodeAt(0)), so one comparison dispatches every harness with no
    // per-harness case branch.
    function _selectHarness(key) {
        var catalog = root._catalog;
        for (var i = 0; i < catalog.length; ++i) {
            var entry = catalog[i];
            if (entry.menuKey.charCodeAt(0) !== key)
                continue;
            // A dimmed row absorbs its key: the menu stays open at stage 0
            // rather than advancing to an action stage that could not spawn.
            if (!entry.available)
                return;
            root.harness = entry.name;
            root.stage = 1;
            return;
        }
    }

    function _spawn(spawnType, dangerous) {
        // dismiss(), not a bare `visible = false`: hiding without emitting
        // `dismissed` skips Main.qml's _restoreCentralFocus, and when the
        // controller declines the spawn — pool full, harness unavailable —
        // nothing else ever grants focus, so the keyboard is left homeless
        // behind a menu that is no longer there. The happy path is
        // unaffected: _restoreCentralFocus lands on the agent surface,
        // which spawn_agent has already switched to and focused.
        //
        // Dispatch FIRST, then dismiss. The other order made the no-op case
        // indistinguishable from the successful one, because the menu was
        // already gone before the controller had a say.
        controller.spawn_agent(spawnType, dangerous, root.harness);
        root.dismiss();
    }

    function _resume(dangerous) {
        var entry = root._entry(root.harness);
        if (entry && entry.resumeRequiresId) {
            // No picker flag in this CLI — hand off to the IDE's session
            // picker, which spawns `<resume flag> <id>` on accept.
            root.visible = false;
            root.resumePickerRequested(root.harness, dangerous);
            return;
        }
        root._spawn("resume", dangerous);
    }

    // Esc is handled by ModalOverlay (→ handleEscape above); everything else
    // lands here already accepted (the modal swallows it).
    onKeyPressed: function (event) {
        if (root.stage === 0) {
            root._selectHarness(event.key);
            return;
        }
        switch (event.key) {
        case Qt.Key_A:
            if (root.vpsMode) {
                root.visible = false;
                root.attachPickerRequested(!(event.modifiers & Qt.ShiftModifier));
            }
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

    // Header — chosen-harness logo + title, centered as a unit. The logo
    // appears only at stage 1, where a harness has actually been chosen;
    // at stage 0 the marks live in the rows themselves. Source comes from
    // the catalog, so a new harness brings its own logo with it. No arrow —
    // the title is a plain "Spawn agent #N" label (numbering is dense
    // display order, so a new agent always becomes #(count + 1)).
    Row {
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: Theme.spacing.sm

        Image {
            // Named so the behavioural probe can assert on THIS icon rather
            // than on "some pi mark somewhere in the subtree" — the looser
            // check passed on a stage-0 row and proved nothing about the
            // header.
            objectName: "headerIcon"
            anchors.verticalCenter: parent.verticalCenter
            visible: root.stage === 1 && root.harness !== ""
            // Brand fills are baked into the SVGs (Claude #D97757 /
            // OpenCode #6f9bd6 / Pi #ffffff) and identify the backend
            // regardless of theme — intentionally NOT Theme-tokened. Paths
            // are relative to this QML file.
            // Resolved explicitly — see the row Image below for why a bare
            // JS string does not become an absolute url here.
            source: {
                var entry = root._entry(root.harness);
                return entry ? Qt.resolvedUrl(entry.icon) : "";
            }
            // Rasterize the vector at 2× the display box so the glyph
            // stays crisp at this small size.
            sourceSize.width: root._headerIconSize * 2
            sourceSize.height: root._headerIconSize * 2
            width: root._headerIconSize
            height: root._headerIconSize
            smooth: true
            fillMode: Image.PreserveAspectFit
        }

        Text {
            objectName: "headerTitle"
            anchors.verticalCenter: parent.verticalCenter
            // Stage-aware, because the two stages ask different questions and
            // a single "Spawn agent #N" title made stage 0 read as though the
            // next keypress spawned something. Both stages carry the number,
            // so the slot being filled stays visible across the transition.
            text: (root.stage === 0
                   ? "Choose harness for agent #"
                   : root.vpsMode ? "Spawn VPS agent #" : "Spawn agent #")
                  + root._nextAgentNumber
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

    // The stage's rows — one single-letter key each. Keys are single
    // monospace glyphs, so the label column self-aligns without a fixed-width
    // key cell.
    //
    // Sits DIRECTLY in ModalOverlay's content Column (the default property):
    // a positioner ignores a zero-sized child, so the Repeater itself adds no
    // gap, and its delegates are stacked into the same Column right after it,
    // keeping header → rows → footer order. The wrapper Column this used to
    // carry only re-declared the outer Column's own spacing.
    Repeater {
        model: root._rows

        Row {
            id: entryRow
            required property var modelData
            spacing: Theme.spacing.sm

            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: entryRow.modelData.key
                // Colour alone marks an unavailable harness — see the
                // note on the removed opacity factor near the top.
                color: entryRow.modelData.dim
                       ? Theme.color.text.dim
                       : Theme.color.text.strong
                font.family: Theme.font.family
                font.pixelSize: Theme.font.size.xs
                font.weight: Theme.font.weight.medium
                renderType: Text.NativeRendering
            }
            Image {
                // Action rows carry no `icon` key at all; undefined is
                // falsy, so the same delegate serves both stages.
                visible: !!entryRow.modelData.icon
                anchors.verticalCenter: parent.verticalCenter
                // Resolved explicitly: a url property fed from a JS string
                // keeps the relative form, so the Image would look for
                // "assets/…" against the wrong base.
                source: entryRow.modelData.icon
                        ? Qt.resolvedUrl(entryRow.modelData.icon)
                        : ""
                sourceSize.width: Theme.font.size.xs * 2
                sourceSize.height: Theme.font.size.xs * 2
                width: visible ? Theme.font.size.xs : 0
                height: Theme.font.size.xs
                smooth: true
                fillMode: Image.PreserveAspectFit
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: entryRow.modelData.label
                color: entryRow.modelData.dim
                       ? Theme.color.text.dim
                       : Theme.color.text.normal
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
    // now the unadvertised default). It names what Esc does HERE, which is
    // stage-dependent.
    Text {
        anchors.horizontalCenter: parent.horizontalCenter
        text: (root.stage === 1 && !root.vpsMode)
              ? "Esc → back"
              : "Esc → cancel"
        color: Theme.color.text.dim
        font.family: Theme.font.family
        font.pixelSize: Theme.font.size.xs
        renderType: Text.NativeRendering
    }
}
