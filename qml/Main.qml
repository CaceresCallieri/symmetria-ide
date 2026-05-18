// Application root window.
// Hosts the NvimView filling most of the window. Two chrome strips
// bracket the content area: AgentTopBar at the top (always-on agent
// dock — pool topology visible in editor mode AND agent mode so the
// user can see every running agent at all times) and StatusBar at
// the bottom (mode/file/branch/pos — replaces NeoVim's lualine).
// No mouse-based interactions — focus always sits on whichever pane
// is currently visible so keystrokes flow straight there.

import QtQuick
import QtQuick.Window
import QtQuick.Layouts

import Symmetria.Ide 1.0
import Symmetria.FileManager.UI as FmUi
import "design"

Window {
    id: root
    width: 1280
    height: 720
    visible: true
    title: "Symmetria IDE"
    // Transparent clear so the compositor shows the wallpaper through
    // the editor viewport (matches Ghostty + other transparent terminals
    // on Hyprland). The status bar and cmdline overlay are opaque —
    // they paint `Theme.color.bg.chrome` (Symmetria Shell matte-pill)
    // on top. See `qml/design/Theme.qml` for the palette source.
    color: "transparent"
    minimumWidth: 800
    minimumHeight: 400

    // ---------------- IDE-wide application shortcuts ----------------
    //
    // Anchor toggle. `Qt.ApplicationShortcut` makes this fire regardless
    // of which pane has focus — NvimView, FileTreeView, AgentPane, future
    // terminal pane — all of them. Anchor is an IDE-level concern (not
    // a nvim or terminal concept), so a `<leader>`-style Lua keybind
    // would mis-locate it; an application-scope shortcut is the right
    // primitive.
    //
    // Qt resolves application shortcuts in QApplication::notify BEFORE
    // the focused widget's keyPressEvent runs, so this wins over
    // NvimView's keyboard capture — Ctrl+Shift+A does NOT leak to nvim
    // even when the editor is focused and in insert mode. If a future
    // regression breaks that ordering, the symptom is "anchor seems to
    // be inserting characters in nvim"; the fix is upstream of this
    // file (a parent widget intercepting the chord) and not a binding
    // change here.
    //
    // Toggle semantics — same key in both directions — keeps the binding
    // surface minimal and matches how the user will think about the
    // feature ("am I anchored or not"). Anchored state is surfaced via
    // `controller.anchored` so a future affordance (file-tree title
    // badge, status-bar pill) is a single binding away from here.
    Shortcut {
        sequences: ["Ctrl+Shift+A"]
        context: Qt.ApplicationShortcut
        onActivated: {
            if (controller.anchored) {
                controller.release_anchor();
            } else {
                controller.anchor_to_current_cwd();
            }
        }
    }

    // Phase 2.5 central-surface swap chords. Same application-scope
    // pattern as Ctrl+Shift+A — they fire regardless of which pane has
    // focus, including from inside nvim's insert mode. The anchor block
    // above documents the QApplication::notify ordering rationale in
    // full; the same reasoning applies here.
    //
    // Two distinct chords (not a single toggle) per the codebase's
    // "each IDE concept is its own chord" precedent. Ctrl+Shift+T
    // summons the terminal; Ctrl+Shift+E summons the editor. The
    // slots are idempotent — pressing the chord that matches the
    // already-visible surface is a no-op (no signal, no QML re-bind).
    Shortcut {
        sequences: ["Ctrl+Shift+T"]
        context: Qt.ApplicationShortcut
        onActivated: controller.swap_to_terminal()
    }

    Shortcut {
        sequences: ["Ctrl+Shift+E"]
        context: Qt.ApplicationShortcut
        onActivated: controller.swap_to_editor()
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // Always-on agent dock at the top. Surfaces the multi-instance
        // pool topology (one bubble per slot, focused/active/empty
        // states) so the user can see every running agent regardless
        // of whether the editor or the agent pane is currently active.
        // Mirrors StatusBar's height + chrome at the bottom — together
        // they bracket the main content with matched chrome strips.
        AgentTopBar {
            id: agentTopBar
            Layout.fillWidth: true
            Layout.preferredHeight: Theme.size.statusBarHeight
        }

        // Editor / agent view swap PLUS always-on file-tree sidebar.
        //
        // Outer RowLayout: `mainContent` (the NvimView | AgentPane
        // visibility-swap) takes fillWidth; a 1px separator + a
        // fixed-width FileTreeView pinned to the right give the user
        // persistent observability into the project layout. The
        // sidebar stays visible across BOTH editor mode and agent
        // mode — per the "visualization-first" decision; users want
        // the structural map at all times, not just while editing.
        // Chrome bars (AgentTopBar above, StatusBar below) bracket
        // the entire row, including the sidebar, for visual continuity.
        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            Item {
                id: mainContent
                Layout.fillWidth: true
                Layout.fillHeight: true

                NvimView {
                    id: editor
                    anchors.fill: parent
                    // Phase 2.5: editor is now ONE of two central surfaces
                    // (the other is the terminal pane below). Visibility
                    // requires BOTH "agent is not full-window overlaid"
                    // AND "central surface is editor". `editorVisible`
                    // is the boolean derivation of `controller.centralSurface`
                    // — see AppController for the state machine.
                    visible: !controller.agentVisible && controller.editorVisible
                    backend: nvimBackend
                    focus: visible

                    Component.onCompleted: forceActiveFocus()
                    onVisibleChanged: if (visible)
                        forceActiveFocus()

                    // Floating cmdline + wildmenu overlay — parented to the
                    // editor so it clips within the viewport (not over the
                    // status bar) and so its anchors.fill tracks editor resizes.
                    // Focus stays on the NvimView; keys flow to NeoVim, which
                    // emits ext_cmdline/ext_popupmenu events that this overlay
                    // reads via cmdlineState / popupmenuModel.
                    CommandLine {
                        id: cmdlineOverlay
                        anchors.fill: parent
                    }

                    // Native which-key overlay. Bottom-anchored inside the
                    // editor so it visually sits above the status bar and
                    // animates alongside editor resizes. Driven entirely by
                    // `whichKeyState` + `whichKeyModel`; Lua side controls
                    // show/hide via rpcnotify (see runtime/lua/orchestrator/
                    // whichkey/init.lua).
                    WhichKeyOverlay {
                        id: whichKeyOverlay
                        anchors.left: editor.left
                        anchors.right: editor.right
                        anchors.bottom: editor.bottom
                        // Clamp to half the viewport so huge menus never hog
                        // the whole editor; scroll support is a v2 follow-up.
                        height: Math.min(implicitHeight, editor.height * 0.5)
                        z: 20
                    }
                }

                // Phase 2.5 terminal pane — sibling of NvimView under
                // `mainContent`. Both panes are anchored to fill the
                // same slot and gated on a `visible` binding; mutual
                // exclusivity is guaranteed at the AppController layer
                // (`centralSurface` is a single string, the two derived
                // booleans are XOR by construction). Pre-warmed via
                // `_terminal_backend.start(cwd)` in AppController.start()
                // so the first paint shows a live shell prompt, not a
                // blank pane — same Q1-1b pattern as nvim.
                //
                // Same FocusScope pattern as NvimView: `focus: visible`
                // plus `Component.onCompleted: forceActiveFocus()` plus
                // `onVisibleChanged` re-grab. When the Ctrl+Shift+T
                // chord toggles `terminalVisible` from false to true,
                // onVisibleChanged grabs keyboard focus so the user can
                // type immediately without an extra click.
                TerminalView {
                    id: terminalView
                    anchors.fill: parent
                    visible: !controller.agentVisible && controller.terminalVisible
                    backend: terminalBackend
                    focus: visible

                    Component.onCompleted: if (visible)
                        forceActiveFocus()
                    onVisibleChanged: if (visible)
                        forceActiveFocus()
                }

                AgentPane {
                    id: agentPane
                    anchors.fill: parent
                    visible: controller.agentVisible
                }
            }

            // 1px vertical separator between editor and sidebar.
            // Visibility tracks the sidebar so a future hide-tree
            // toggle reclaims the pixel cleanly.
            Rectangle {
                Layout.fillHeight: true
                implicitWidth: 1
                visible: controller.treeVisible
                color: FmUi.FmTheme.palette.outlineVariant
            }

            // File-tree sidebar.
            //
            // FocusScope wrapper carries focus into the internal
            // ListView when the user presses <leader>tf. The
            // ListView inside FileTreeView has `focus: true`
            // (FileTreeView.qml:493), so once this FocusScope joins
            // the active focus chain the ListView becomes its focus
            // delegate and its Keys.onPressed block receives j/k/h/l.
            //
            // Earlier this FocusScope had `focus: false` to block the
            // ListView's startup `view.forceActiveFocus()` from
            // stealing focus from the editor. That wall worked for
            // startup BUT also blocked our explicit <leader>tf focus
            // grants — the FocusScope refused to ever enter the focus
            // chain, so arrow keys went nowhere even after focus_tree
            // fired. Replaced with a one-shot Window-level startup
            // override below (`Component.onCompleted: editor.
            // forceActiveFocus()`) that runs AFTER all child
            // Component.onCompleted handlers, giving the editor the
            // final word on initial focus without permanently
            // disabling our FocusScope. See the startup focus override
            // comment at Window.Component.onCompleted below.
            //
            // Visibility defaults to true; no toggle keybind in v1
            // per the "visualization-first" decision.
            FocusScope {
                id: treeScope
                Layout.minimumWidth: 280
                Layout.maximumWidth: 280
                Layout.fillHeight: true
                visible: controller.treeVisible

                // Ctrl+H reverse-spillover: focus is in the tree, user
                // wants to go back to the editor.
                //
                // Implemented as a window-scoped Shortcut rather than
                // Keys.onPressed because FileTreeView's internal ListView
                // captures the key event before it can bubble up to this
                // FocusScope: the ListView's own `Keys.onPressed` matches
                // `event.key === Qt.Key_H` without checking modifiers
                // (the standard vim-style pattern), so it treats Ctrl+H
                // as plain `h` (collapse node) and accepts the event,
                // halting propagation. `Keys.priority: BeforeItem` on
                // this FocusScope does NOT solve that — BeforeItem orders
                // OUR own handlers relative to OUR own auto-handling, not
                // relative to a descendant focusItem's handlers. Qt
                // always delivers key events to the focused item first.
                //
                // Shortcut bypasses focus-chain delivery entirely. The
                // `enabled: treeScope.activeFocus` gate scopes the
                // binding to "focus is somewhere inside the tree's
                // subtree" — for a FocusScope, `activeFocus` is true
                // when ANY descendant has the active focus, which is
                // exactly the scope we want.
                //
                // Other directions (Ctrl+J/K/L) currently no-op from
                // the tree: nothing above, below, or right of it. Adding
                // an agent dock down the road extends this with another
                // Shortcut entry.
                Shortcut {
                    sequence: "Ctrl+H"
                    enabled: treeScope.activeFocus
                    context: Qt.WindowShortcut
                    onActivated: controller.focus_editor()
                }

                // Panel-level chrome matte. Painted on the FocusScope itself
                // (not each child) so the spacing gap between GitStatusPanel
                // and FileTreeView — and any future sub-panels — inherits the
                // background without the desktop wallpaper bleeding through.
                // Uses `Theme.color.bg.chrome`, the same token GitStatusPanel
                // uses for its own framed background, so the two panels read
                // as one continuous dark column. (§3 P1: chrome → Theme.*)
                Rectangle {
                    anchors.fill: parent
                    color: Theme.color.bg.chrome
                }

                // Two-section composition inside the side panel:
                //   1. GitStatusPanel — auto-hidden when clean; collapses
                //      to zero height so the tree below claims its space.
                //   2. FileTreeView   — fills the remaining vertical space.
                //
                // No separator between them — the panel's chrome border
                // already provides visual delineation, and a hairline
                // separator would just add noise when the panel hides.
                ColumnLayout {
                    anchors.fill: parent
                    // No explicit spacing between GitStatusPanel and
                    // FileTreeView. Both already contribute ~10px of
                    // intra-component padding (GitStatusPanel's
                    // `anchors.bottomMargin: Theme.spacing.sm` + the FM
                    // ListView's `anchors.margins: FmTheme.padding.sm`),
                    // which is enough visual breath to distinguish the
                    // two surfaces. Adding `spacing.lg` on top of that
                    // (previous setup, commit 59d602a) produced a
                    // ~26px band that read as "blank wallpaper" rather
                    // than panel separation. When GitStatusPanel is
                    // hidden (clean tree) the layout naturally collapses
                    // — `spacing` only applies between visible siblings,
                    // so 0 is the cleanest no-op in that state too.
                    // If you want them visually further apart, raise
                    // this — but check the cumulative gap, not just
                    // this value in isolation.
                    spacing: 0

                    GitStatusPanel {
                        id: gitStatusPanel
                        Layout.fillWidth: true
                        model: gitStatusList
                        // Same activation contract as the FileTreeView: open
                        // the path in nvim and re-focus the editor so the
                        // user can immediately start editing. Routed through
                        // `controller.open_in_nvim` — the same slot the FM
                        // overlay and the tree's onFileActivated already use.
                        onFileActivated: function (path) {
                            controller.open_in_nvim(path);
                            if (editor.visible)
                                editor.forceActiveFocus();
                        }
                    }

                    FmUi.FileTreeView {
                        id: fileTreeView
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        // `displayedRoot` (NOT raw `cwd`) so the tree pins
                        // to the anchored project root when the user has
                        // anchored. When unanchored, `displayedRoot` is
                        // identical to `cwd` — no behavior change from the
                        // pre-anchor world. The Ctrl+Shift+A application-
                        // scope Shortcut at the Window root toggles the
                        // anchor; `:SymmetriaAnchor` / `:SymmetriaUnanchor`
                        // are the scripted surface for the same Slots.
                        rootPath: controller.displayedRoot
                        respectGitignore: true
                        // Information-density mode: shrinks row height, icons,
                        // fonts, indent, and inter-element spacing to 60% of
                        // the FM's default size so more files fit per
                        // viewport (target: neo-tree-style compactness for
                        // the IDE sidebar). The FM picker overlay
                        // (`<C-u>` → fmOverlayLoader below) keeps the
                        // default 1.0 — picker rows benefit from the larger
                        // hit target since they're transient and one-shot.
                        compactScale: 0.8
                        // -1 = fully recursive expand at mount; FM caps at
                        // maxExpandDepth=8 (default) plus internal guardrails
                        // (.git skip, 200-children fanout, 10k row ceiling).
                        initialExpandDepth: -1
                        // Git status badges. The FM's `statusProvider` is a
                        // duck-typed seam (property var) — it calls our
                        // adapter's `statusForPath(absolutePath)` per visible
                        // row and re-binds on `statusChanged`. We use
                        // `gitProviderAdapter` rather than `gitController`
                        // directly because the FM expects
                        // `{char, color, tooltip}` (a resolved color), while
                        // `GitController` returns `{char, state, tooltip}`
                        // (a state name). The adapter maps state→FmTheme
                        // color so the IDE never hardcodes hex values from
                        // the FM palette.
                        statusProvider: gitProviderAdapter
                        onFileActivated: function (path) {
                            controller.open_in_nvim(path);
                            if (editor.visible)
                                editor.forceActiveFocus();
                        }
                    }
                }
            }
        }

        Connections {
            target: controller
            function onFocusTreeRequested(): void {
                // FileTreeView's outer root is a plain Item (NOT a
                // FocusScope), so calling forceActiveFocus() on it
                // only makes the OUTER ITEM the activeFocusItem —
                // the internal ListView (which owns Keys.onPressed
                // for j/k/h/l navigation) never receives the focus,
                // and arrow keys go nowhere even after <leader>tf.
                //
                // Walk fileTreeView's descendants to find the
                // ListView and call forceActiveFocus() directly on
                // it, which mirrors what FileTreeView does internally
                // (FileTreeView.qml:493 — `view.forceActiveFocus()`
                // in the ListView's Component.onCompleted).
                //
                // Slightly hacky — depends on FileTreeView keeping a
                // single ListView descendant. The clean long-term
                // fix is for the FM to expose a public
                // `focusInternal()` method we can call. File as a
                // Phase 2 follow-up.
                var listView = _findListView(fileTreeView);
                if (listView)
                    listView.forceActiveFocus();
                else
                    fileTreeView.forceActiveFocus();  // safety fallback
            }

            // Reverse direction of onFocusTreeRequested. Fired from
            // AppController._on_nav_event (nvim spillover with dir
            // matching the editor in the focus chain) and from the
            // tree's Ctrl+H handler. NvimView itself IS a FocusScope
            // (NvimView.qml manages its own focus), so a direct
            // forceActiveFocus on `editor` lands the focus correctly
            // without the descendant-walker workaround needed for the
            // tree direction.
            function onFocusEditorRequested(): void {
                editor.forceActiveFocus();
            }

            // Phase 2.5 terminal focus pull. Fired from
            // AppController.focus_terminal() — currently called only
            // by Ctrl+Shift+T's swap_to_terminal slot via its own
            // onVisibleChanged trigger, but the signal exists so a
            // future spillover surface (e.g. tree's Ctrl+L when terminal
            // sits to the right) can pull focus without going through
            // the swap path. TerminalView is its own FocusScope, so a
            // direct forceActiveFocus works (no descendant-walker needed).
            function onFocusTerminalRequested(): void {
                terminalView.forceActiveFocus();
            }

            // WORKAROUND: recursive descendant walker using toString() type detection.
            // Root cause: FileTreeView's outer Item is not a FocusScope and exposes no
            // public focusInternal() method, so we walk children to find the ListView.
            // Remove once FM exposes FocusScope or a public focusView() slot.
            // `toString()` on a QML object returns a class-name-prefixed string like
            // "QQuickListView_QML_NN(0x...)" — checking the prefix
            // is the most portable way to identify the type from
            // QML without importing private Qt headers.
            function _findListView(item: var): var {
                if (!item || !item.children)
                    return null;
                for (var i = 0; i < item.children.length; i++) {
                    var c = item.children[i];
                    if (c && c.toString && c.toString().indexOf("ListView") >= 0)
                        return c;
                    var nested = _findListView(c);
                    if (nested)
                        return nested;
                }
                return null;
            }
        }

        StatusBar {
            id: statusBar
            Layout.fillWidth: true
            Layout.preferredHeight: Theme.size.statusBarHeight
        }
    }

    // ------------------------------------------------------------------
    // File manager toggle-overlay.
    //
    // Imported as `FmUi` (alias) from Symmetria.FileManager.UI to avoid
    // singleton-name collision with the IDE's own Theme: both modules
    // export a `Theme`/`FmTheme` singleton, and the alias keeps the
    // FM's symbols in their own namespace so this file can mention
    // `Theme` (IDE) and `FmUi.FmTheme` (FM) without ambiguity.
    //
    // Loader.active toggles per visibility — the panel is reconstructed
    // on each show. An earlier "keep loaded" approach (`active: visible
    // || item !== null`) preserved tab/scroll/selection state across
    // toggles, but it conflicted with the FM's focus-on-construction
    // pattern: FileList.view grabs active focus inside its
    // `Component.onCompleted` hook (FileList.qml:221), which only fires
    // once per construction. After we hand focus back to the editor on
    // dismiss, a subsequent show couldn't re-route focus into `view`
    // (the FM panel's root is Item, not FocusScope, so focus
    // restoration doesn't propagate from a parent forceActiveFocus).
    // For picker-mode use (each <C-u> is a fresh "open file" flow),
    // losing tab/scroll between toggles is acceptable; the
    // ~50-100ms reconstruction cost is also acceptable for a binding
    // that fires on user keypress, not in any hot path.
    Loader {
        id: fmOverlayLoader
        anchors.fill: parent
        z: 100
        active: controller.fmVisible

        // Start picker mode when the overlay first opens. The panel reuses
        // its existing picker infrastructure (built for the XDG portal) as
        // a clean "select a file" affordance: confirming a selection emits
        // FileManagerService.pickerCompleted; cancelling emits
        // pickerCancelled. We connect to both below — no fifoPath is
        // passed, so the panel's standalone-host FIFO writer is dormant.
        onLoaded: {
            FmUi.FileManagerService.startPickerMode({
                title: "Open File",
                acceptLabel: "Open"
            });
        }

        // When the overlay closes (controller.fmVisible flips to false),
        // also clear picker mode so the panel returns to its idle state.
        // Without this, re-opening the overlay would pile a second
        // startPickerMode call on top of an already-active picker.
        Connections {
            target: controller
            function onFmVisibleChanged(): void {
                // Cancel any in-flight picker mode when the overlay
                // closes. FileManagerService is a singleton — its state
                // outlives the Loader's reconstruction cycle, so without
                // this the next show would skip startPickerMode and
                // the panel would have no way to emit pickerCompleted.
                // Note: no fmOverlayLoader.item guard — under per-show
                // reconstruction, item is null exactly when we need to
                // cancel.
                if (!controller.fmVisible && FmUi.FileManagerService.pickerMode) {
                    FmUi.FileManagerService.cancelPickerMode();
                }
                // startPickerMode on show is handled by the Loader's
                // onLoaded handler below — fires on every reconstruction.

                // ----------------------------------------------------------------
                // Focus return on dismiss. Without this, focus stays on
                // the now-destroyed fmOverlay subtree's parent and
                // keystrokes go nowhere — nvim/agent appears frozen
                // until alt-tab. Mirrors the priority ordering in
                // Window.onActiveChanged below: agent if visible,
                // otherwise editor.
                if (!controller.fmVisible) {
                    if (controller.agentVisible)
                        agentPane.forceActiveFocus();
                    else if (controller.terminalVisible)
                        terminalView.forceActiveFocus();
                    else
                        editor.forceActiveFocus();
                }
            }
        }

        // Bridge picker completion → nvim :edit. The signal fires whether
        // the user pressed Enter on a file or the panel auto-completed
        // (e.g. Shift+Enter copy-then-confirm flow). pick_in_nvim does
        // the fnameescape + :edit AND dismisses the overlay — distinct
        // from open_in_nvim (used by the sidebar's onFileActivated)
        // which keeps the sidebar visible after activation.
        Connections {
            target: FmUi.FileManagerService
            function onPickerCompleted(fifoPath: string, paths: var): void {
                if (paths && paths.length > 0)
                    controller.pick_in_nvim(paths[0]);
                else
                    controller.hide_fm();
            }
            function onPickerCancelled(fifoPath: string): void {
                controller.hide_fm();
            }
        }

        sourceComponent: Item {
            id: fmOverlay
            anchors.fill: parent
            // No visible/focus/onVisibleChanged bindings needed: this Item
            // only exists while controller.fmVisible is true (Loader.active
            // tears it down on hide). forceActiveFocus on construction is
            // all that is required.
            Component.onCompleted: forceActiveFocus()

            Keys.onEscapePressed: event => {
                controller.hide_fm();
                event.accepted = true;
            }

            // Bare `q` also dismisses — IDE-specific UX glue, not panel
            // default. The FM panel's NormalModeHandler.js only handles
            // Ctrl+Q (close-tab); bare `q` falls through unhandled and
            // bubbles up to here. `Qt.NoModifier` guard means Ctrl+Q
            // still routes to the panel's tab logic. Keys.onPressed
            // fires AFTER child items, so any future FM mode that wants
            // to consume `q` (e.g. inline rename) just sets
            // event.accepted = true and our handler skips.
            Keys.onPressed: event => {
                if (event.key === Qt.Key_Q && event.modifiers === Qt.NoModifier) {
                    controller.hide_fm();
                    event.accepted = true;
                }
            }

            // Dim scrim — clicking dismisses.
            Rectangle {
                anchors.fill: parent
                color: "#000000"
                opacity: 0.45

                Behavior on opacity {
                    NumberAnimation {
                        duration: 120
                    }
                }

                MouseArea {
                    anchors.fill: parent
                    onClicked: controller.hide_fm()
                }
            }

            // Telescope-style centered panel.
            FmUi.FileManager {
                id: fmPanel
                anchors.centerIn: parent
                width: parent.width * 0.8
                height: parent.height * 0.8
                // initialPath flips between empty (overlay closed) and the
                // controller's resolved path (overlay open). Setting on
                // close clears panel state for the next open — see
                // controller.hide_fm which resets _fm_initial_path = "".
                initialPath: controller.fmInitialPath || ""
                onCloseRequested: controller.hide_fm()
            }
        }
    }

    // ----------------------------------------------------------------
    // Git-status adapter — translates GitController's payload to the
    // FM's `statusProvider` contract.
    // ----------------------------------------------------------------
    //
    // GitController (Python side) returns `{char, state, tooltip}` where
    // `state` is a semantic name ("unstaged", "staged", "untracked", …).
    // The FM's `FileTreeView.statusProvider` contract expects
    // `{char, color, tooltip}` where `color` is a resolved colour value.
    //
    // This adapter is a thin QtObject that:
    //   1. Forwards `statusForPath(absolute)` calls to `gitController`.
    //   2. Maps the returned state name to the corresponding
    //      `FmUi.FmTheme.gitStatus.*` palette value — so badge colours
    //      stay consistent with whatever the FM ships, and we never
    //      hardcode hex values on the IDE side.
    //   3. Re-emits `statusChanged` (the signal FM listens for) so the
    //      file tree invalidates its delegate bindings in one pass.
    //
    // The duck-typed FM seam (`property var statusProvider: null`) means
    // we don't need to inherit from any interface — just match the shape.
    QtObject {
        id: gitProviderAdapter

        signal statusChanged

        function statusForPath(absolutePath) {
            var s = gitController.statusForPath(absolutePath);
            // GitController returns {} for clean files / paths outside the
            // repo. The FM treats null and empty objects identically (no
            // badge); we return null for symmetry with the contract docs.
            if (!s || !s.char)
                return null;
            return {
                char: s.char,
                color: _colorForState(s.state),
                tooltip: s.tooltip
            };
        }

        function _colorForState(state) {
            switch (state) {
            case "unstaged":
                return FmUi.FmTheme.gitStatus.unstagedRed;
            case "staged":
                return FmUi.FmTheme.gitStatus.stagedGreen;
            case "untracked":
                return FmUi.FmTheme.gitStatus.untrackedBlue;
            case "renamed":
                return FmUi.FmTheme.gitStatus.renamedYellow;
            case "conflicted":
                return FmUi.FmTheme.gitStatus.conflictedMagenta;
            case "ignored":
                return FmUi.FmTheme.gitStatus.ignoredGray;
            default:
                return FmUi.FmTheme.gitStatus.unstagedRed;
            }
        }
    }

    // Forward the controller's emit so the FM's
    // `Connections.ignoreUnknownSignals: true` block picks it up and
    // invalidates badge bindings in one pass per scan.
    //
    // Lives as a sibling rather than a child of the adapter because
    // QtObject has no default property (only Item, FocusScope, etc. do),
    // so child objects can only be attached as named properties — which
    // would obscure the signal-forwarding intent. A sibling Connections
    // with an explicit `target: gitController` is the canonical pattern.
    Connections {
        target: gitController
        function onStatusChanged(): void {
            gitProviderAdapter.statusChanged();
        }
    }

    // Focus handoff when the window regains activation: whichever
    // view is currently visible grabs focus, so alt-tabbing back
    // never leaves the user typing into a dead surface.
    //
    // The `&& fmOverlayLoader.item` guard is still required under the
    // per-show Loader.active reconstruction model — Qt does not
    // guarantee that `Loader.active: controller.fmVisible` (a binding)
    // and this `Window.onActiveChanged` (a signal handler) are
    // delivered in the same frame. If activation fires while the
    // Loader is mid-reconstruction, `.item` can momentarily be null
    // even with `controller.fmVisible == true`. Falling through to the
    // editor branch in that frame is benign — `onFmVisibleChanged`
    // will reassert FM focus on the next tick when the Loader settles.
    onActiveChanged: {
        if (!active)
            return;
        if (controller.fmVisible && fmOverlayLoader.item)
            fmOverlayLoader.item.forceActiveFocus();
        else if (controller.agentVisible)
            agentPane.forceActiveFocus();
        else if (controller.terminalVisible)
            terminalView.forceActiveFocus();
        else
            editor.forceActiveFocus();
    }

    // Startup focus override. Component.onCompleted fires
    // bottom-up: child handlers run before parent handlers, so by
    // the time THIS handler fires every child Component.onCompleted
    // — including FileTreeView's internal ListView grab at
    // FileTreeView.qml:493 — has already run. Asserting
    // `editor.forceActiveFocus()` here is the final word on initial
    // focus, replacing the previous `focus: false` wall on the
    // tree's FocusScope (which permanently broke the tree's ability
    // to receive focus via <leader>tf). See gotcha #16 in CLAUDE.md
    // for the related "deferred callbacks don't fire during
    // prefix-wait" rule on the Lua side — this is the QML-side
    // analog: don't fight nested Component.onCompleted with
    // declaratively-disabled FocusScopes, fight it with one
    // post-construction explicit grant.
    Component.onCompleted: {
        // Q2-d topology — first launch shows the terminal as the persistent
        // home surface. Pre-PR-5 this hardcoded `editor.forceActiveFocus()`;
        // PR 4's `_central_surface = "terminal"` default + this dispatch
        // pull the focus into whichever surface is actually visible.
        // The terminal pane's `Component.onCompleted` ALSO grabs focus on
        // its own first construction, but this Window-level handler is
        // the final word per the gotcha #16 / QML-side analog argument
        // documented above for the editor branch.
        if (controller.terminalVisible)
            terminalView.forceActiveFocus();
        else
            editor.forceActiveFocus();
    }
}
