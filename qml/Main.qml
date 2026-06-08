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

    // Side-panel sub-pane focus memory. 0 = main fileTreeView, 1 =
    // gitStatusPanel (Active Changes). Updated by the Ctrl+J / Ctrl+K
    // chords below; read by `onFocusTreeRequested` so re-entering the
    // side panel from a central pane (via Ctrl+L) lands focus on
    // whichever sub-pane the user was last in, NOT always on the main
    // tree. This gives the two sub-panes true parity — the user can
    // "live in" the changes pane across editor round-trips without
    // re-navigating each time. Default 0 preserves prior behavior on
    // first Ctrl+L. Auto-reset to 0 when gitStatusPanel hides (clean
    // tree → invisible item can't accept focus; without the reset,
    // Ctrl+L would silently bind focus to a hidden item).
    property int activeTreeSubPane: 0

    // Editor minimap feature flag. Temporarily OFF (2026-06-08) to give
    // the nvim-in-terminal editor a minimal, base-usable state. The whole
    // minimap pipeline still runs (runtime/lua/orchestrator/minimap.lua →
    // MinimapModel → MinimapView); only the QML surface is hidden, and the
    // editor reclaims the reserved 80px strip automatically because its
    // `anchors.rightMargin` keys off `minimap.visible`. Flip to `true` to
    // restore the minimap — no other change needed. See docs/minimap-prd.md.
    readonly property bool minimapEnabled: false

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

    // Phase 2.5 central-surface toggle chord. Same application-scope
    // pattern as Ctrl+Shift+A — fires regardless of which pane has
    // focus, including from inside nvim's insert mode. The anchor block
    // above documents the QApplication::notify ordering rationale in
    // full; the same reasoning applies here.
    //
    // Single toggle, not two distinct chords. Earlier iteration shipped
    // Ctrl+Shift+T (→ terminal) + Ctrl+Shift+E (→ editor) under the
    // "each IDE concept is its own chord" precedent, but day-to-day
    // ergonomics didn't bear it out for a binary swap. Now: Ctrl+Shift+E
    // flips between editor and terminal — pressing from a non-editor
    // surface lands you on the editor; pressing from the editor returns
    // you to the terminal. See AppController.toggle_editor_terminal for
    // the asymmetry rationale (chord names the editor, so non-editor →
    // editor is the dominant direction).
    Shortcut {
        sequences: ["Ctrl+Shift+E"]
        context: Qt.ApplicationShortcut
        onActivated: controller.toggle_editor_terminal()
    }

    // IDE-wide file-manager toggle. Promoted out of the nvim layer
    // (previously `<leader>e` / `<C-u>` via `runtime/init.lua`'s hijack)
    // so the FM opens uniformly from any pane — editor, agent, terminal,
    // tree sidebar — without depending on nvim having focus. Same
    // ApplicationShortcut + Qt.ApplicationShortcut pattern as the swap
    // chords above. Opens at the anchored project root (or cached cwd when
    // unanchored), the same path the file-tree and git panes use.
    //
    // Trade-off: Qt.ApplicationShortcut intercepts before NvimView.keyPressEvent,
    // so nvim's built-in `<C-e>` (scroll viewport down one line) is silently
    // consumed when the editor pane is focused. Same precedence as Ctrl+Shift+T
    // and Ctrl+Shift+E (see CLAUDE.md swap-chords entry). Accepted: FM-from-any-
    // pane uniformity is worth the nvim scroll key loss for this project.
    Shortcut {
        sequences: ["Ctrl+E"]
        context: Qt.ApplicationShortcut
        onActivated: controller.toggle_fm()
    }

    // IDE-wide horizontal pane navigation.
    //
    // Spatial chord: Ctrl+H = move left, Ctrl+L = move right. The
    // window has a two-column topology — a central surface on the
    // left (agent / editor / terminal, one visible at a time) and
    // the file-tree sidebar on the right — so:
    //
    //   - Ctrl+L from any central surface         -> focus tree
    //   - Ctrl+H from the tree                    -> focus visible central
    //   - Ctrl+L from the tree (no right neighbor) -> silent no-op
    //   - Ctrl+H from a central (no left neighbor) -> silent no-op
    //
    // Application-scope chord per the project-wide principle in
    // `.claude/memory/project/meta/ide_owns_keybind_layer.md`: IDE
    // owns horizontal navigation; nvim/terminal/agent are demoted
    // to bare engines and never see the chord (Qt resolves
    // ApplicationShortcut in QApplication::notify BEFORE the focused
    // widget's keyPressEvent, exactly like Ctrl+Shift+A — see the
    // anchor block above for the full ordering rationale).
    //
    // Why bare Ctrl+H/L, not Ctrl+Shift+H/L: matches the user's
    // existing vim-tmux-navigator muscle memory. The cost is
    // terminal Ctrl+H (= ASCII Backspace) and Ctrl+L (= clear
    // screen) being unreachable inside the terminal pane — TUIs
    // that bind those literals separately lose them. Regular
    // Backspace is unaffected. Accepted tradeoff per the design
    // discussion on 2026-05-20.
    //
    // Capture pitfall preserved from the previous tree-scoped
    // Ctrl+H Shortcut: FileTreeView's internal ListView matches
    // `event.key === Qt.Key_H` WITHOUT a modifier check, so
    // without an external Shortcut interception, Ctrl+H gets eaten
    // as plain `h` (collapse node) and never reaches a focus-chain
    // handler at the FocusScope level. ApplicationShortcut bypasses
    // focus-chain delivery entirely, sidestepping that descendant
    // capture. Do NOT replace this with a focus-chain handler
    // (Keys.onPressed on treeScope, Keys.priority: BeforeItem,
    // etc.) — Qt always delivers key events to the focused item
    // first, and BeforeItem orders OUR handlers vs OUR auto-handling,
    // not vs a descendant focusItem's handlers.
    //
    // Why the chord lives in QML (not as `controller.navigate_left/
    // right` slots in Python): all the state it needs to consult
    // — `treeScope.activeFocus`, `agentVisible`, `terminalVisible`,
    // `treeVisible` — is already declarative QML state plus
    // controller properties. Round-tripping through a Python slot
    // would just push the dispatch logic across the JS/Python
    // boundary for no benefit.
    Shortcut {
        sequences: ["Ctrl+H"]
        context: Qt.ApplicationShortcut
        onActivated: {
            if (!treeScope.activeFocus)
                return;
            // FM is a co-mounted central-pane sibling — when it is
            // visible, editor/terminal/agent are all gated off by
            // !controller.fmVisible and cannot hold activeFocus.
            // Route back to the FM item directly; fall through to
            // the three-way dispatch below only when FM is not open.
            if (controller.fmVisible && fmPaneLoader.item)
                fmPaneLoader.item.forceActiveFocus();
            else if (controller.agentVisible)
                agentPane.forceActiveFocus();
            else if (controller.terminalVisible)
                terminalView.forceActiveFocus();
            else
                editor.forceActiveFocus();
        }
    }

    Shortcut {
        sequences: ["Ctrl+L"]
        context: Qt.ApplicationShortcut
        onActivated: {
            if (treeScope.activeFocus)
                return;
            if (!controller.treeVisible)
                return;
            controller.focus_tree();
        }
    }

    // IDE-wide vertical sub-pane navigation INSIDE the side panel.
    //
    // The side panel is sub-divided into two co-mounted FileTreeViews:
    // the Active Changes pane (gitStatusPanel, sits on top, hidden when
    // the working tree is clean) and the main FileTreeView below. Both
    // own identical FM-level Keys.onPressed handlers (j/k/Ctrl+D/
    // Ctrl+U/Return/...), so once focus lands on either inner ListView
    // the keys all "just work" inside that sub-pane — the only thing
    // missing was a way to ROUTE focus between the two sub-panes.
    //
    // Spatial chord, vim-style: Ctrl+K = up (the changes pane is
    // physically above), Ctrl+J = down (the main tree is below).
    // Directional, NOT toggle — Ctrl+K from the main tree always lands
    // on the changes pane; Ctrl+K when already in the changes pane is a
    // silent no-op. This is the same shape as Ctrl+H / Ctrl+L for
    // horizontal cross-pane nav (always-directional, never-wrap), so
    // the muscle memory is consistent.
    //
    // CRITICAL gating: `enabled: treeScope.activeFocus` — and for Ctrl+K
    // ALSO `gitStatusPanel.visible`. When the side panel doesn't have
    // focus (e.g. focus is on editor, terminal, or agent pane), the
    // Shortcut is `enabled: false` and Qt does NOT consume the key —
    // it passes through the focus chain normally. That preserves:
    //   - nvim's Ctrl+K (no default binding; many plugins use it)
    //   - terminal Ctrl+J (= ASCII LF, literal newline; critical for
    //     readline/zsh/anything that reads stdin)
    //   - terminal Ctrl+K (= readline kill-to-end-of-line)
    // When the side panel DOES have focus, these meanings would be
    // unreachable anyway (you're not typing into nvim/terminal here),
    // so the chord interception is the only sensible behavior.
    //
    // Why ApplicationShortcut not Keys.onPressed at treeScope: the FM's
    // inner ListView matches `event.key === Qt.Key_J/K` WITHOUT a
    // modifier check (FileTreeView.qml:1024,1030) — bare j/k for
    // next/prev row. Without ApplicationShortcut interception, Ctrl+J
    // would be eaten by the ListView handler as plain `j` (advance
    // row) before any focus-chain handler could see it. Same rationale
    // as the Ctrl+H Shortcut comment block above; the precedent is
    // already canonical here.
    //
    // gitStatusPanel.visible gate on Ctrl+K is important: when the
    // working tree is clean the changes pane is `visible: false` and
    // its inner items can't accept focus. Without the gate, Ctrl+K
    // would land focus on an invisible item — focus would silently
    // disappear and the user couldn't navigate anywhere with keys
    // until they clicked or pressed Ctrl+H back to a central pane.
    Shortcut {
        sequences: ["Ctrl+K"]
        context: Qt.ApplicationShortcut
        enabled: treeScope.activeFocus && gitStatusPanel.visible
        onActivated: {
            // Optimistic update — onActiveFocusChanged (gitStatusPanel) also writes
            // this property, but we set it here as a defensive fallback in case
            // focusInternal() doesn't land focus (e.g. item not yet ready).
            root.activeTreeSubPane = 1;
            gitStatusPanel.focusInternal();
        }
    }

    Shortcut {
        sequences: ["Ctrl+J"]
        context: Qt.ApplicationShortcut
        // No gitStatusPanel.visible guard here — intentional asymmetry with Ctrl+K.
        // Ctrl+J must stay enabled even when the changes pane is hidden, so the
        // user can still reach the main tree. If we added the guard, a user in the
        // changes pane when it hides (clean tree) would have no keyboard path to the
        // main tree until a Ctrl+H+Ctrl+L round-trip.
        enabled: treeScope.activeFocus
        onActivated: {
            // Optimistic update — onActiveFocusChanged (mainTreeScope) also writes
            // this property, but we set it here as a defensive fallback in case
            // focusInternal() doesn't land focus (e.g. item not yet ready).
            root.activeTreeSubPane = 0;
            fileTreeView.focusInternal();
        }
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

                // Editor surface: a TerminalView hosting `nvim --listen` as a
                // TUI (editorBackend). nvim renders its grid here; the IDE
                // chrome (cmdline/which-key/minimap/status) is fed by rpcnotify
                // relays over the RPC socket the Python NvimBackend attaches to.
                TerminalView {
                    id: editor
                    anchors.fill: parent
                    // Phase 0 (editor minimap, docs/minimap-prd.md): the
                    // minimap (declared as a sibling below) occupies a
                    // fixed-width ribbon on mainContent's right edge when
                    // it is visible. Reserving that strip via rightMargin
                    // keeps the editor TerminalView's visible boundary correct — the
                    // grid shrinks by minimap.width when the minimap is on,
                    // expands back to the full slot when the minimap hides
                    // (e.g. when a future per-buffer toggle disables it).
                    // TerminalView and AgentPane do NOT carry this margin
                    // because the minimap is gated on `editorVisible` — it
                    // is hidden whenever those panes are active, so they
                    // legitimately fill the full slot.
                    anchors.rightMargin: minimap.visible ? minimap.width : 0
                    // Phase 2.5: editor is now ONE of two central surfaces
                    // (the other is the terminal pane below). Visibility
                    // requires BOTH "agent is not full-window overlaid"
                    // AND "central surface is editor". `editorVisible`
                    // is the boolean derivation of `controller.centralSurface`
                    // — see AppController for the state machine.
                    //
                    // Also gated on `!controller.fmVisible` — the file
                    // manager is co-mounted as a sibling pane below and,
                    // when active, fully occupies the central slot (no
                    // overlay/scrim anymore). The three panes form an
                    // editor / terminal / agent / FM XOR cluster.
                    visible: !controller.agentVisible && !controller.fmVisible && controller.editorVisible
                    backend: editorBackend
                    focus: visible

                    Component.onCompleted: if (visible)
                        forceActiveFocus()
                    onVisibleChanged: if (visible)
                        forceActiveFocus()

                    // Floating cmdline + wildmenu overlay — parented to the
                    // editor so it clips within the viewport (not over the
                    // status bar) and so its anchors.fill tracks editor resizes.
                    // Focus stays on the editor TerminalView; keys flow to nvim via
                    // PTY. NeoVim relays the cmdline via vim.ui_attach over the RPC
                    // socket; this overlay reads it via cmdlineState / completionModel.
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
                // plus `Component.onCompleted: if (visible) forceActiveFocus()` plus
                // `onVisibleChanged` re-grab. When the Ctrl+Shift+T
                // chord toggles `terminalVisible` from false to true,
                // onVisibleChanged grabs keyboard focus so the user can
                // type immediately without an extra click.
                TerminalView {
                    id: terminalView
                    anchors.fill: parent
                    visible: !controller.agentVisible && !controller.fmVisible && controller.terminalVisible
                    backend: terminalBackend
                    focus: visible

                    Component.onCompleted: if (visible)
                        forceActiveFocus()
                    onVisibleChanged: if (visible)
                        forceActiveFocus()
                }

                // REGRESSION NOTE: a 2026-05-23 experiment wrapped AgentPane
                // in a Loader (`active: controller.agentVisible || item !== null`)
                // to defer its 800-line parse + Symmetria.Ide import cost
                // until first-open. The Loader saved ~12-25ms in
                // `engine_loaded` (measured via SYMMETRIA_IDE_TRACE) but
                // regressed `tree_mount_settled` by 60-120ms on the
                // bambin repo (~2200 files / ~480 dirs). Smaller repos
                // were neutral. Net effect was negative on the dominant
                // case, so the inline form below is correct. Hypothesis:
                // without AgentPane in the eager-evaluation graph, the
                // QML engine's first-frame scheduling lets the file
                // tree's incremental expansion contend differently with
                // FM/terminal warmup work. Reproduce with `bench/measure_mount.py
                // --trace` before attempting another defer pass — and
                // verify on bambin, not just small repos.
                AgentPane {
                    id: agentPane
                    anchors.fill: parent
                    visible: controller.agentVisible && !controller.fmVisible
                }

                // Phase 0 editor minimap (docs/minimap-prd.md). Narrow
                // ribbon anchored to mainContent's right edge, only
                // visible when the editor is the active central surface
                // — same visibility gate as NvimView, so the minimap and
                // its host pane appear/disappear as one. Width comes from
                // `Theme.size.minimapWidth` (Phase 0 default = 80 px).
                //
                // Phase 0 paints a single solid background; Phase 2 adds
                // block-mode line rendering, Phase 3 the click-drag
                // viewport scrubber, Phase 4 the diagnostic/git gutter,
                // and Phase 5 the glyph sprite atlas. The Python class
                // already has the pooled QRectF + memoized QColor in
                // place so each phase can extend `paint()` without
                // re-establishing gotcha #10 hygiene.
                //
                // `scrollPosition` is pinned to 0 — the pixel scroll-spring
                // that fed it died with the grid renderer (nvim renders in
                // the terminal now). The viewport indicator is driven by the
                // `minimap_viewport` rpcnotify channel via the model instead.
                //
                // No focus participation: MinimapView.setActiveFocusOnTab(False)
                // on the Python side, so Tab cycling skips it. Keyboard
                // focus always lands on the editor or sidebar.
                MinimapView {
                    id: minimap
                    anchors.top: parent.top
                    anchors.bottom: parent.bottom
                    anchors.right: parent.right
                    width: Theme.size.minimapWidth
                    // `root.minimapEnabled` is the temporary off-switch (see
                    // the property declaration on the Window root). The three
                    // controller terms preserve the Phase 0 contract: even
                    // when re-enabled, the minimap only shows while the editor
                    // is the active central surface.
                    visible: root.minimapEnabled
                        && !controller.agentVisible
                        && !controller.fmVisible
                        && controller.editorVisible
                    // The grid renderer's pixel scroll-spring is gone (nvim
                    // renders in the terminal now). The viewport indicator is
                    // driven by the minimap_viewport rpcnotify channel via the
                    // model; scrollPosition stays at 0 (Phase 0 made no visible
                    // use of it).
                    scrollPosition: 0
                    // Phase 1: live binding to the minimap content model
                    // populated by runtime/lua/orchestrator/minimap.lua.
                    // Stays at 0 until the Lua side emits its first
                    // snapshot (BufEnter → schedule_snapshot_immediate);
                    // the subscribe-race re-push in NvimBackend ensures
                    // we get a snapshot even if the initial autocmd
                    // fired before Python subscribed.
                    bufferRowCount: minimapModel.lineCount
                    // Phase 2: inject the model itself so MinimapView.paint
                    // can read indent_level() / line_count() directly,
                    // without going through QML property marshalling.
                    // The setter wires linesChanged → update() so any
                    // content mutation repaints the silhouette. Mirrors
                    // the NvimView.backend / TerminalView.backend
                    // injection pattern — keeps the chrome consistent
                    // across panes.
                    model: minimapModel
                    // Above the central panes so the ribbon visibly sits
                    // on top of NvimView's right edge; below
                    // mainContentFocusBorder (z: 50) so the focus
                    // hairline still wraps the whole mainContent slot.
                    z: 10

                    // Phase 3 click + drag scrubber. Routes mouse position
                    // to a buffer row and calls controller.seek_to_row,
                    // which marshals through nvim.async_call to the
                    // pynvim worker thread (gotcha #1 — pynvim isn't
                    // thread-safe; the @Slot side handles the marshalling).
                    //
                    // Throttled at ~16 ms via _seekTimer so a held-drag
                    // doesn't pin the GUI thread emitting one seek per
                    // mousemove event (PRD §7.3 R3.2). The timer
                    // re-targets each tick rather than queuing — the
                    // most recent position is what the user wants.
                    //
                    // anchors.fill on the parent MinimapView captures
                    // the entire ribbon (background + indent silhouette
                    // + viewport indicator) as the scrubbing surface.
                    MouseArea {
                        id: scrubber
                        anchors.fill: parent
                        // Throttle target: the pending row to seek to,
                        // -1 means "no pending seek." Set by the
                        // mouse handlers; consumed (and cleared) by
                        // the timer's tick.
                        property int _pendingRow: -1

                        function _rowFromY(y) {
                            // Map a pixel y inside the minimap to a buffer
                            // row index. The minimap renders rows at
                            // `i * row_h` where row_h = max(_MIN_ROW_HEIGHT_PX,
                            // view_h / line_count) — so the inverse mapping
                            // is `floor(y / row_h)`. We replicate the
                            // row_h formula here rather than exposing it
                            // from the painter: a QML-side re-derivation
                            // keeps the click target consistent with the
                            // visible block layout without a round-trip.
                            const lineCount = minimapModel.lineCount;
                            if (lineCount <= 0)
                                return 0;
                            const naturalH = height / lineCount;
                            // _MIN_ROW_HEIGHT_PX = 2.0 on the Python side;
                            // duplicate here so the QML side computes the
                            // same row index Python would. If either side
                            // tunes the floor, the other must follow —
                            // a drift-detection test pins it.
                            const minRowH = 2.0;
                            const rowH = naturalH < minRowH ? minRowH : naturalH;
                            const idx = Math.floor(y / rowH);
                            if (idx < 0)
                                return 0;
                            if (idx >= lineCount)
                                return lineCount - 1;
                            return idx;
                        }

                        onPressed: function (mouse) {
                            scrubber._pendingRow = scrubber._rowFromY(mouse.y);
                            _seekTimer.restart();
                        }
                        onPositionChanged: function (mouse) {
                            if (!pressed)
                                return;
                            scrubber._pendingRow = scrubber._rowFromY(mouse.y);
                            if (!_seekTimer.running)
                                _seekTimer.restart();
                        }
                        onReleased: function (mouse) {
                            // Fire any pending row immediately on release
                            // so a quick click doesn't get throttled out.
                            if (scrubber._pendingRow >= 0) {
                                controller.seek_to_row(scrubber._pendingRow);
                                scrubber._pendingRow = -1;
                            }
                            _seekTimer.stop();
                        }

                        Timer {
                            id: _seekTimer
                            interval: 16  // ~60 Hz cap
                            repeat: false
                            onTriggered: {
                                if (scrubber._pendingRow >= 0) {
                                    controller.seek_to_row(scrubber._pendingRow);
                                    scrubber._pendingRow = -1;
                                    // If the user is still dragging, the
                                    // next onPositionChanged restarts the
                                    // timer. No need to chain ticks here.
                                }
                            }
                        }
                    }
                }

                // Active-pane focus border. Renders a 1px accent hairline
                // around the visible central surface when any of its three
                // candidate panes (agent / editor / terminal) has the
                // active keyboard focus. When focus moves to the tree or
                // to the FM overlay, the border disappears — the existing
                // 1px static separator between mainContent and treeScope
                // remains as the always-visible delineator, so the layout
                // never gains visual weight in the idle state.
                //
                // Why a sibling Rectangle (not Rectangle.border on each
                // pane): NvimView and TerminalView are Python-side
                // QQuickPaintedItem subclasses that don't expose a
                // `border` property; AgentPane is a QML composite that
                // already has its own internal Rectangle chrome we
                // don't want to wrap. A sibling overlay with
                // `anchors.fill: parent` covers all three panes
                // uniformly via the parent Item's geometry — one
                // binding, one place to tune.
                //
                // Why `color: "transparent"` + conditional border.color
                // (not `border.width: focused ? 1 : 0`): keeps the
                // Rectangle's geometry stable across focus transitions
                // — Qt re-computes anchor children when the bordering
                // item's width/height changes, and a flickering 1px
                // resize would cost a paint round-trip per chord. The
                // transparent-when-inactive approach paints either an
                // accent line or a fully transparent line; the layout
                // never moves.
                //
                // z-order: above the pane siblings in mainContent
                // (editor / terminalView / agentPane / fmPaneLoader are
                // all z: 0 by default, so z: 50 guarantees the border draws
                // on top of their outermost pixel). WhichKeyOverlay is z: 20
                // inside editor, a different stacking context, so no
                // interference there.
                Rectangle {
                    id: mainContentFocusBorder
                    anchors.fill: parent
                    color: "transparent"
                    // FM is treated like a regular central surface for the
                    // focus hairline: when fmVisible is true and the side
                    // panel doesn't own the focus, the FM is the active
                    // pane by elimination (editor/terminal/agent are all
                    // gated off by !fmVisible). Using fmVisible directly
                    // — rather than walking the focus chain into the FM's
                    // internal ListView — keeps the binding declarative
                    // and matches the XOR shape that already governs the
                    // other panes.
                    border.color: (agentPane.paneActive
                                   || editor.activeFocus
                                   || terminalView.activeFocus
                                   || (controller.fmVisible && !treeScope.activeFocus))
                                  ? Theme.color.accent.focus
                                  : "transparent"
                    border.width: 1
                    z: 50
                }

                // File manager — central-pane surface (not an overlay).
                // Was a Window-root Loader at z:100 with a dim scrim
                // covering the whole window; now lives in `mainContent`
                // as a sibling of editor/terminal/agent. The other panes
                // are gated on `!controller.fmVisible` so exactly one
                // central surface is visible at a time, matching the
                // editor/terminal/agent XOR cluster.
                //
                // Loader.active toggles per visibility — the panel is
                // reconstructed on each show. An earlier "keep loaded"
                // approach (`active: visible || item !== null`) preserved
                // tab/scroll/selection state across toggles, but it
                // conflicted with the FM's focus-on-construction pattern:
                // FileList.view grabs active focus inside its
                // `Component.onCompleted` hook (FileList.qml:221), which
                // only fires once per construction. After we hand focus
                // back to the editor on dismiss, a subsequent show
                // couldn't re-route focus into `view` (the FM panel's
                // root is Item, not FocusScope, so focus restoration
                // doesn't propagate from a parent forceActiveFocus). For
                // picker-mode use (each Ctrl+E is a fresh "open file"
                // flow), losing tab/scroll between toggles is acceptable;
                // the ~50-100ms reconstruction cost is also acceptable
                // for a binding that fires on user keypress, not in any
                // hot path.
                Loader {
                    id: fmPaneLoader
                    anchors.fill: parent
                    active: controller.fmVisible

                    // Start picker mode when the panel first opens. The
                    // panel reuses its existing picker infrastructure
                    // (built for the XDG portal) as a clean "select a
                    // file" affordance: confirming a selection emits
                    // FileManagerService.pickerCompleted; cancelling
                    // emits pickerCancelled. We connect to both below —
                    // no fifoPath is passed, so the panel's
                    // standalone-host FIFO writer is dormant.
                    onLoaded: {
                        FmUi.FileManagerService.startPickerMode({
                            title: "Open File",
                            acceptLabel: "Open"
                        });
                    }

                    // When the FM closes (controller.fmVisible flips to
                    // false), also clear picker mode so the panel returns
                    // to its idle state. Without this, re-opening would
                    // pile a second startPickerMode call on top of an
                    // already-active picker.
                    Connections {
                        target: controller
                        function onFmVisibleChanged(): void {
                            if (!controller.fmVisible) {
                                // Cancel any in-flight picker mode when the
                                // panel closes. FileManagerService is a
                                // singleton — its state outlives the Loader's
                                // reconstruction cycle, so without this the
                                // next show would skip startPickerMode and
                                // the panel would have no way to emit
                                // pickerCompleted. Note: no fmPaneLoader.item
                                // guard — under per-show reconstruction, item
                                // is null exactly when we need to cancel.
                                if (FmUi.FileManagerService.pickerMode)
                                    FmUi.FileManagerService.cancelPickerMode();

                                // Focus return on dismiss. Without this,
                                // focus stays on the now-destroyed fmPane
                                // subtree's parent and keystrokes go nowhere
                                // — nvim/agent appears frozen until alt-tab.
                                // Mirrors the priority ordering in
                                // Window.onActiveChanged below: agent if
                                // visible, terminal if visible, otherwise
                                // editor.
                                if (controller.agentVisible)
                                    agentPane.forceActiveFocus();
                                else if (controller.terminalVisible)
                                    terminalView.forceActiveFocus();
                                else
                                    editor.forceActiveFocus();
                            }
                        }
                    }

                    // Bridge picker completion → nvim :edit. The signal
                    // fires whether the user pressed Enter on a file or
                    // the panel auto-completed (e.g. Shift+Enter
                    // copy-then-confirm flow). pick_in_nvim runs the
                    // edit via nvim_cmd RPC AND dismisses the panel —
                    // distinct from open_in_nvim (used by the sidebar's
                    // onFileActivated) which keeps the sidebar visible
                    // after activation.
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
                        id: fmPane
                        anchors.fill: parent
                        // No forceActiveFocus() on construction. Children's
                        // Component.onCompleted runs BEFORE parents' — so
                        // FileList's ListView inside the FM subtree has
                        // already claimed activeFocus via its own
                        // `view.forceActiveFocus()` (FileList.qml:245 in
                        // the installed module) by the time we get here.
                        // A parent-level forceActiveFocus would STEAL
                        // focus from the ListView onto fmPane (a plain
                        // Item, not a FocusScope, so focus stops here and
                        // never propagates back down) — which is exactly
                        // what broke arrow-key navigation once the Lua
                        // `<C-u>` path was retired in favor of an
                        // IDE-wide Ctrl+E ApplicationShortcut. Esc and
                        // bare-q still dismiss: Esc is handled inside the
                        // panel's NormalModeHandler (cancels picker mode
                        // → hide_fm); bare-q isn't accepted by the panel
                        // (it only consumes Ctrl+Q), so the unaccepted
                        // KeyEvent bubbles up to the Keys handlers below.

                        Keys.onEscapePressed: event => {
                            controller.hide_fm();
                            event.accepted = true;
                        }

                        // Bare `q` also dismisses — IDE-specific UX glue,
                        // not panel default. The FM panel's
                        // NormalModeHandler.js only handles Ctrl+Q
                        // (close-tab); bare `q` falls through unhandled
                        // and bubbles up to here. `Qt.NoModifier` guard
                        // means Ctrl+Q still routes to the panel's tab
                        // logic. Keys.onPressed fires AFTER child items,
                        // so any future FM mode that wants to consume `q`
                        // (e.g. inline rename) just sets event.accepted =
                        // true and our handler skips.
                        Keys.onPressed: event => {
                            if (event.key === Qt.Key_Q && event.modifiers === Qt.NoModifier) {
                                controller.hide_fm();
                                event.accepted = true;
                            }
                        }

                        // FM panel fills the pane. Previously this was
                        // anchored to centerIn with width/height at 80%
                        // of parent, on top of a dim scrim — both gone
                        // now that the FM is the central surface rather
                        // than a modal overlay. No scrim, no MouseArea
                        // click-to-dismiss: dismissal is via Esc, bare-q,
                        // Ctrl+E (toggle), or picker
                        // completion/cancellation.
                        FmUi.FileManager {
                            id: fmPanel
                            anchors.fill: parent
                            // initialPath flips between empty (FM
                            // closed) and the controller's resolved path
                            // (FM open). Setting on close clears panel
                            // state for the next open — see
                            // controller.hide_fm which resets
                            // _fm_initial_path = "".
                            initialPath: controller.fmInitialPath || ""
                            onCloseRequested: controller.hide_fm()
                        }
                    }
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
            // override below (Window.Component.onCompleted) that runs
            // AFTER all child Component.onCompleted handlers, giving
            // the active central surface the final word on initial
            // focus without permanently disabling our FocusScope.
            // See the startup focus override comment at
            // Window.Component.onCompleted below.
            //
            // Visibility defaults to true; no toggle keybind in v1
            // per the "visualization-first" decision.
            FocusScope {
                id: treeScope
                Layout.minimumWidth: 280
                Layout.maximumWidth: 280
                Layout.fillHeight: true
                visible: controller.treeVisible

                // Tree-scoped Ctrl+H was previously installed here as a
                // Qt.WindowShortcut to handle "tree has focus, user wants
                // editor". That responsibility has moved up to the
                // application-scope Ctrl+H Shortcut at the Window root —
                // same dispatch rationale (ListView eats `h` without
                // modifier check, focus-chain handlers can't intercept
                // in time), now applied uniformly across every pane
                // boundary instead of just tree→editor. See the
                // Ctrl+H / Ctrl+L Shortcut block at the Window root.

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

                // Whole-side-panel focus border removed — replaced by
                // per-sub-pane focus indicators inside each sub-pane
                // (see the Rectangle children of GitStatusPanel and the
                // mainTreeScope FocusScope below). A single envelope
                // around the entire side panel couldn't communicate
                // WHICH sub-pane (changes vs main tree) had focus, which
                // mattered once Ctrl+J/Ctrl+K subdivided the side panel
                // into two independently-navigable regions. The per-pane
                // borders use the same 1px hairline / accent-on-focus /
                // transparent-otherwise contract as `mainContentFocusBorder`.

                // Three-section composition inside the side panel:
                //   1. LocationHeader — current displayedRoot + anchor
                //      glyph. Always visible (the "where am I" question
                //      is load-bearing for the dual-mode navigation-vs-
                //      project framing in docs/vision.md).
                //   2. GitStatusPanel — auto-hidden when clean; collapses
                //      to zero height so the tree below claims its space.
                //   3. FileTreeView   — fills the remaining vertical space.
                //
                // No separator between them — the panel's chrome border
                // already provides visual delineation, and a hairline
                // separator would just add noise when GitStatusPanel hides.
                ColumnLayout {
                    anchors.fill: parent
                    // No explicit spacing between sub-sections. Each
                    // contributes its own intra-component padding (header
                    // has `anchors.margins`, GitStatusPanel has
                    // `anchors.bottomMargin: Theme.spacing.sm`, the FM
                    // ListView has `anchors.margins: FmTheme.padding.sm`),
                    // which is enough visual breath to distinguish them.
                    // Adding `spacing.lg` on top of that (previous setup,
                    // commit 59d602a) produced a ~26px band that read as
                    // "blank wallpaper" rather than panel separation. When
                    // GitStatusPanel is hidden (clean tree) the layout
                    // naturally collapses — `spacing` only applies between
                    // visible siblings, so 0 is the cleanest no-op too.
                    // If you want them visually further apart, raise this
                    // — but check the cumulative gap, not just this value
                    // in isolation.
                    spacing: 0

                    // Side-panel "where am I" header. Binds
                    // `controller.displayedRootCompact` (HOME-collapsed)
                    // and `controller.anchored` — both are reactive @Property
                    // bindings, so cd-ing in the terminal or pressing
                    // Ctrl+Shift+A updates this header without any extra
                    // wiring. Anchored state surfaces two ways:
                    //   (a) text color flips from text.normal → accent.primary
                    //       so the header reads as "this is committed work"
                    //       vs "I'm just looking around"
                    //   (b) a small accent dot appears to the right of the path
                    // Redundancy is intentional — color alone fails on the
                    // edge case where the user has reduced palette saturation
                    // at the compositor level. See docs/vision.md "Modes of
                    // inhabiting the IDE" for the framing this surface
                    // operationalizes.
                    Rectangle {
                        id: locationHeader
                        Layout.fillWidth: true
                        Layout.preferredHeight: Theme.size.statusBarHeight
                        color: Theme.color.bg.chrome

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: Theme.spacing.sm
                            anchors.rightMargin: Theme.spacing.sm
                            spacing: Theme.spacing.xs

                            Text {
                                id: locationLabel
                                text: controller.displayedRootCompact
                                color: controller.anchored
                                       ? Theme.color.accent.primary
                                       : Theme.color.text.normal
                                font.family: editorFontFamily
                                font.pixelSize: Theme.font.size.sm
                                font.weight: controller.anchored
                                             ? Theme.font.weight.medium
                                             : Theme.font.weight.normal
                                // ElideMiddle keeps the leading `~` AND
                                // the trailing project basename visible
                                // when the path overflows — both ends are
                                // the most informative bits ("where it
                                // is" + "what it is"). ElideRight would
                                // hide the basename, ElideLeft hides the
                                // ~-anchor and reads as a stray subpath.
                                elide: Text.ElideMiddle
                                verticalAlignment: Text.AlignVCenter
                                Layout.fillWidth: true
                            }

                            // Anchor dot — appears only when anchored.
                            // Width/height tied to the spacing token so
                            // it scales if the panel chrome is ever
                            // re-tuned. Radius = half = circle.
                            Rectangle {
                                id: anchorDot
                                Layout.preferredWidth: Theme.spacing.xs * 2
                                Layout.preferredHeight: Theme.spacing.xs * 2
                                Layout.alignment: Qt.AlignVCenter
                                radius: width / 2
                                color: Theme.color.accent.primary
                                visible: controller.anchored
                            }
                        }
                    }

                    GitStatusPanel {
                        id: gitStatusPanel
                        Layout.fillWidth: true
                        // Cap the changes pane at half the side panel
                        // column so a pathological changeset (e.g. a
                        // fresh checkout with hundreds of untracked
                        // files, or a project with a giant `node_modules`
                        // surfaced via `respectGitignore: false`) cannot
                        // push the main FileTreeView below off-screen.
                        // When the cap engages, the panel's embedded
                        // FileTreeView scrolls via its own ListView.
                        // `parent` is the enclosing ColumnLayout, whose
                        // height is anchored to the side panel
                        // Rectangle — stable, no binding loop.
                        // TODO: promote to Theme.sizing.gitPanelMaxFraction
                        // once this pattern recurs (Theme token is the right
                        // escape hatch per project-standards §3 P2).
                        maxHeight: parent.height * 0.5
                        model: gitStatusList
                        // Header-bucket aggregates (staged/unstaged/untracked
                        // adds/dels/files) — populated by the same worker
                        // scan as the file list. Binding to a `@Property
                        // notify=statsChanged` on the Python side means QML
                        // re-evaluates whenever the worker publishes a new
                        // scan, no manual refresh wiring needed.
                        stats: gitController.stats
                        // Drive the embedded FileTreeView's rootPath +
                        // status badges + pathFilter. All three derive
                        // from the same scan as `stats`, so they re-emit
                        // in the same `gc.disable` window on the Python
                        // side — QML sees one consistent snapshot per
                        // scan, no inter-prop tearing.
                        repoRoot: gitController.repoRoot
                        statusProvider: gitProviderAdapter
                        pathFilter: gitController.changedPathSet
                        // Same activation contract as the main FileTreeView:
                        // open the path in nvim and re-focus the editor so
                        // the user can immediately start editing. Routed
                        // through `controller.open_in_nvim` — the same slot
                        // the FM overlay and the main tree's onFileActivated
                        // already use.
                        onFileActivated: function (path) {
                            controller.open_in_nvim(path);
                            if (editor.visible)
                                editor.forceActiveFocus();
                        }

                        // GitStatusPanel's root is a FocusScope, so
                        // `activeFocus` is true whenever the embedded
                        // inner ListView has activeFocus — works for
                        // BOTH keyboard chord arrivals (Ctrl+K via
                        // `focusInternal()`) and mouse clicks (the FM
                        // delegate focuses the ListView naturally).
                        // The sticky `activeTreeSubPane` property gets
                        // updated here too, so Ctrl+L re-entry from the
                        // editor lands here even after a click-driven
                        // focus arrival — no click-vs-keyboard desync.
                        onActiveFocusChanged: {
                            if (activeFocus)
                                root.activeTreeSubPane = 1;
                        }

                        // Per-sub-pane focus indicator. Replaces the
                        // prior whole-side-panel `treeScopeFocusBorder`
                        // — the user couldn't tell WHICH sub-pane had
                        // focus from a single envelope around the whole
                        // column. Same 1px hairline / transparent-when-
                        // inactive contract as `mainContentFocusBorder`,
                        // just scoped to this individual sub-pane.
                        // border.color flips (not border.width) to keep
                        // the geometry stable — toggling width costs a
                        // layout round-trip that's visibly janky during
                        // focus transitions.
                        Rectangle {
                            anchors.fill: parent
                            color: "transparent"
                            border.color: gitStatusPanel.activeFocus
                                          ? Theme.color.accent.focus
                                          : "transparent"
                            border.width: 1
                            z: 50
                        }
                    }

                    // Wrap the main FileTreeView in a FocusScope so its
                    // `activeFocus` propagates from the inner ListView,
                    // mirroring GitStatusPanel's FocusScope-rooted
                    // behavior. This is what lets the per-sub-pane
                    // focus border + sticky-property tracking work for
                    // the main tree too — `mainTreeScope.activeFocus`
                    // is true on click OR keyboard arrival. Layout
                    // properties (fillWidth/fillHeight) live on the
                    // wrapper; the FileTreeView itself uses anchors.fill
                    // to match the wrapper's geometry.
                    FocusScope {
                        id: mainTreeScope
                        Layout.fillWidth: true
                        Layout.fillHeight: true

                        onActiveFocusChanged: {
                            if (activeFocus)
                                root.activeTreeSubPane = 0;
                        }

                        FmUi.FileTreeView {
                            id: fileTreeView
                            anchors.fill: parent
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
                            // Pre-computed ignored-path set from the IDE's
                            // GitController. Lets the FM short-circuit its
                            // per-directory `git check-ignore --stdin`
                            // shell pipeline (sequential, ~30–40ms per dir),
                            // which dominated tree-mount time on
                            // medium-to-large repos (bambin: ~4s → target
                            // sub-100ms). The GitController computes this
                            // in a single `git ls-files --others --ignored
                            // --exclude-standard --directory` pass per
                            // status scan. Falls back to gitignoreSvc when
                            // null (initial state before the first scan
                            // completes), so the first-frame tree may
                            // briefly use the slow path before the
                            // GitController emits.
                            ignoredPathSet: gitController.ignoredPathSet
                            // Information-density mode: shrinks row height, icons,
                            // fonts, indent, and inter-element spacing to 60% of
                            // the FM's default size so more files fit per
                            // viewport (target: neo-tree-style compactness for
                            // the IDE sidebar). The FM central-pane surface
                            // (Ctrl+E → fmPaneLoader inside mainContent)
                            // keeps the default 1.0 — picker rows benefit
                            // from the larger hit target since they're
                            // transient and one-shot.
                            compactScale: 0.8
                            // Viewport-driven (lazy) auto-expand. Replaces the
                            // earlier `initialExpandDepth: -1` cascade, which
                            // eagerly instantiated up to 100 FileSystemModel +
                            // QFileSystemWatcher pairs at mount (cap-tripping
                            // on bambin-scale repos for ~30 user-visible
                            // rows). lazyExpand fills the viewport once at
                            // mount, then extends one directory at a time as
                            // the user scrolls toward the bottom of the
                            // rendered content. The Active Changes panel
                            // below keeps `initialExpandDepth: -1` because
                            // pathFilter already narrows it to the
                            // changeset.
                            lazyExpand: true
                            // Per-project expanded-state cache (option 6). The
                            // controller loads the saved set from disk
                            // synchronously on every `displayedRootChanged` —
                            // BEFORE the FM's `onRootPathChanged` cascade
                            // runs — so this binding holds the right list
                            // by the time the FM decides which expansion
                            // mode to use. Empty list (no cache yet) =>
                            // falls through to `lazyExpand`. Non-empty =>
                            // restores the saved tree shape, bypassing the
                            // lazy cascade for this mount. See
                            // `tree_state_cache.py` + AppController's
                            // `_sync_expanded_paths_cache` for the
                            // load/save contract.
                            restoreExpandedPaths: controller.expandedPathsCache
                            // Persist every user-driven expand/collapse so
                            // the next session restores the same shape.
                            // The signal is SUPPRESSED during a restore
                            // cycle (FileTreeView's `_emitExpandedState`
                            // guards on `_restoreActive`) so a replay
                            // doesn't churn the disk-write path.
                            onExpandedStateChanged: function (paths) {
                                controller.saveExpandedPaths(paths);
                            }
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

                        // Per-sub-pane focus indicator for the main
                        // FileTreeView. Symmetric counterpart of
                        // GitStatusPanel's overlay border above. Bound
                        // to `mainTreeScope.activeFocus` (the wrapping
                        // FocusScope), which flips when the inner
                        // ListView gains/loses activeFocus.
                        Rectangle {
                            anchors.fill: parent
                            color: "transparent"
                            border.color: mainTreeScope.activeFocus
                                          ? Theme.color.accent.focus
                                          : "transparent"
                            border.width: 1
                            z: 50
                        }
                    }
                }
            }
        }

        Connections {
            target: controller
            function onFocusTreeRequested(): void {
                // Honors the sticky `activeTreeSubPane` property — Ctrl+L
                // re-entry lands focus on whichever sub-pane the user
                // last navigated to (via Ctrl+J / Ctrl+K), not always
                // on the main tree. The visibility guard handles the
                // race where the changes pane was last-active but
                // hid mid-session before this signal fired (clean
                // tree); in that case we fall back to the main tree.
                //
                // Both panes expose `focusInternal()` — a public
                // function on the FM FileTreeView that delegates
                // `forceActiveFocus()` to its internal `view` ListView
                // (the item that actually owns `Keys.onPressed` for
                // j/k/h/l/Ctrl+D/Ctrl+U/Return). Calling
                // `forceActiveFocus()` on the FileTreeView's outer Item
                // directly is a no-op for keyboard nav — the outer
                // Item becomes activeFocusItem but keystrokes never
                // reach the ListView. GitStatusPanel forwards through
                // the same `focusInternal()` surface so both sub-panes
                // are structurally symmetric.
                if (root.activeTreeSubPane === 1 && gitStatusPanel.visible)
                    gitStatusPanel.focusInternal();
                else
                    fileTreeView.focusInternal();
            }

            // Reverse direction of onFocusTreeRequested. Fired from
            // AppController._on_nav_event (nvim spillover with dir
            // matching the editor in the focus chain).
            // NOTE: the Ctrl+H ApplicationShortcut at the Window root
            // calls `editor.forceActiveFocus()` directly and does NOT
            // fire this signal — keep in sync if adding new nav targets.
            // NvimView itself IS a FocusScope (NvimView.qml manages its
            // own focus), so a direct forceActiveFocus on `editor` lands
            // correctly without the descendant-walker workaround needed
            // for the tree direction.
            function onFocusEditorRequested(): void {
                editor.forceActiveFocus();
            }

            // Phase 2.5 terminal focus pull. Fired from
            // AppController.focus_terminal() — called from internal
            // slots (e.g. swap_to_terminal's onVisibleChanged trigger
            // path when reached via the Ctrl+Shift+E toggle), and
            // the signal exists so a future Lua nvim-spillover surface
            // can request terminal focus without going through the
            // swap path.
            // NOTE: the Ctrl+H / Ctrl+L ApplicationShortcuts call
            // `terminalView.forceActiveFocus()` directly and do NOT
            // go through this signal.
            // TerminalView is its own FocusScope, so a direct
            // forceActiveFocus works (no descendant-walker needed).
            function onFocusTerminalRequested(): void {
                terminalView.forceActiveFocus();
            }

        }

        // Auto-reset `activeTreeSubPane` when the changes pane hides
        // (clean working tree). Without this, the sticky-focus property
        // could point at an invisible sub-pane — Ctrl+L would then call
        // `gitStatusPanel.focusInternal()` and the FM would
        // forceActiveFocus on an item Qt refuses to focus (invisible
        // items can't be activeFocusItem), silently dropping focus
        // into a black hole. Resetting to 0 (main tree) keeps Ctrl+L
        // always landing on a reachable sub-pane. The Ctrl+K chord
        // that originally set activeTreeSubPane=1 is also gated on
        // `gitStatusPanel.visible`, so the symmetric guard there
        // prevents the bad state from being re-entered while the
        // pane is hidden.
        Connections {
            target: gitStatusPanel
            function onVisibleChanged(): void {
                if (gitStatusPanel.visible || root.activeTreeSubPane !== 1)
                    return;
                root.activeTreeSubPane = 0;
                // If the side panel currently has focus, the previously-
                // active sub-pane is the one that just hid — its inner
                // items can no longer hold activeFocus. Hand focus to
                // the main tree so the user can keep navigating without
                // having to round-trip through Ctrl+H+Ctrl+L.
                if (treeScope.activeFocus)
                    fileTreeView.focusInternal();
            }
        }

        StatusBar {
            id: statusBar
            Layout.fillWidth: true
            Layout.preferredHeight: Theme.size.statusBarHeight
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
            // `adds` / `dels` populate the FileTreeView delegate's inline
            // `+adds -dels` accessory. They're always present on the
            // GitStatus payload (default 0); rows with 0/0 render no
            // accessory by the FM's own visibility guard, so we don't
            // need to gate them here. The main FileTreeView consumes the
            // same statusProvider; for unchanged paths it never sees this
            // branch (returns null above).
            return {
                char: s.char,
                color: _colorForState(s.state),
                tooltip: s.tooltip,
                adds: s.additions,
                dels: s.deletions
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
    // The `&& fmPaneLoader.item` guard is still required under the
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
        if (controller.fmVisible && fmPaneLoader.item)
            fmPaneLoader.item.forceActiveFocus();
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
        //
        // FM guard mirrors onActiveChanged priority: fmPaneLoader.item is
        // null at startup (FM is never the initial surface), so this branch
        // is purely defensive — it ensures the two handlers stay symmetric
        // if startup state ever includes fmVisible.
        if (controller.fmVisible && fmPaneLoader.item)
            fmPaneLoader.item.forceActiveFocus();
        else if (controller.terminalVisible)
            terminalView.forceActiveFocus();
        else
            editor.forceActiveFocus();
    }
}
