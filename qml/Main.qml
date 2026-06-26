// Application root window.
// Hosts the editor (a QMLTermWidget running nvim) filling most of the
// window. Two chrome strips
// bracket the content area: AgentTopBar at the top (always-on agent
// dock — pool topology visible in editor mode AND agent mode so the
// user can see every running agent at all times) and StatusBar at
// the bottom (mode/file/branch/pos — replaces NeoVim's lualine).
// No mouse-based interactions — focus always sits on whichever pane
// is currently visible so keystrokes flow straight there.

import QtQuick
import QtQuick.Window
import QtQuick.Layouts

import QMLTermWidget 2.0

import Symmetria.Ide 1.0
import Symmetria.FileManager.UI as FmUi
import "design"
import "githistory"

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

    // Responsive sidebar: report live window width to the controller so it
    // can auto-hide the file-tree sidebar on narrow layouts (Hyprland tiling
    // a half-monitor column, etc.). The controller folds this into
    // `treeVisible` (SIDEBAR_MIN_WINDOW_WIDTH in app.py) and only flips the
    // bound bindings when visibility crosses the threshold. The initial
    // width is reported from the startup `Component.onCompleted` below —
    // `onWidthChanged` does not fire for the construction-time value.
    onWidthChanged: controller.set_window_width(width)

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

    // Editor minimap feature flag.
    // HACK: disabled pending nvim-in-terminal minimap integration — the
    // original MinimapView painted a silhouette driven by the nvim grid
    // renderer, which is gone now that nvim runs inside a QMLTermWidget.
    // Remove once MinimapView reconnects to the terminal viewport (see
    // docs/minimap-prd.md). The pipeline still runs; only the QML surface
    // is gated off. Flip to `true` to restore — no other change needed.
    readonly property bool minimapEnabled: false

    // ---------------- IDE-wide application shortcuts ----------------
    //
    // Anchor toggle. `Qt.ApplicationShortcut` makes this fire regardless
    // of which pane has focus — editor, FileTreeView, AgentPane, future
    // terminal pane — all of them. Anchor is an IDE-level concern (not
    // a nvim or terminal concept), so a `<leader>`-style Lua keybind
    // would mis-locate it; an application-scope shortcut is the right
    // primitive.
    //
    // Qt resolves application shortcuts in QApplication::notify BEFORE
    // the focused widget's keyPressEvent runs, so this wins over
    // the editor terminal's keyboard capture — the chord does NOT leak to nvim
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
    //
    // P-for-pin: this chord was Ctrl+Shift+A until 2026-06-10, when A
    // was reassigned to the agent namespace (the spawn menu below) —
    // A-for-agent beat A-for-anchor in the user's mental model.
    Shortcut {
        sequences: ["Ctrl+Shift+P"]
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
    // pattern as the anchor chord above — fires regardless of which pane has
    // focus, including from inside nvim's insert mode. The anchor block
    // above documents the QApplication::notify ordering rationale in
    // full; the same reasoning applies here.
    //
    // A toggle, not a one-way chord. Earlier iteration shipped
    // Ctrl+Shift+T (→ terminal) + Ctrl+Shift+E (→ editor) under the
    // "each IDE concept is its own chord" precedent, but day-to-day
    // ergonomics didn't bear out two distinct chords for a binary swap.
    // Now: Ctrl+Shift+E flips between the editor and where you came from —
    // pressing from a non-editor surface lands you on the editor; pressing
    // from the editor returns you BACK to your previous surface (not
    // hard-coded terminal). See AppController.toggle_editor_terminal for the
    // swap-last-two rationale (chord names the editor, so non-editor → editor
    // is the forward direction; editor → back is the return). Every surface
    // chord (E/T/G/B) shares this shape now — see toggle_terminal_home below.
    Shortcut {
        sequences: ["Ctrl+Shift+E"]
        context: Qt.ApplicationShortcut
        onActivated: controller.toggle_editor_terminal()
    }

    // Terminal chord — 'T' names the terminal home base. Now a toggle like
    // the others: pressing from any other surface → terminal; pressing while
    // already on the terminal → BACK to the surface you came from
    // (toggle_terminal_home → _surface_back_target). The terminal is no longer
    // a privileged forced-fallback — every surface chord swaps between your
    // two most-recent surfaces (single previous-pointer; vim Ctrl-^ / alt-tab
    // semantics). swap_to_terminal stays the one-way "go home, no bounce"
    // primitive for callers (file-open, tests) that need it.
    Shortcut {
        sequences: ["Ctrl+Shift+T"]
        context: Qt.ApplicationShortcut
        onActivated: controller.toggle_terminal_home()
    }

    // Git-history viewer toggle. 'G' names the history surface, so this
    // always lands you there unless you were already on it, in which case
    // it returns you BACK to the surface you came from (not hard-coded
    // terminal — same swap-last-two shape as Ctrl+Shift+E/T). The viewer is
    // the read-only comprehension surface for catching up on agent-authored
    // commits — see qml/githistory/.
    Shortcut {
        sequences: ["Ctrl+Shift+G"]
        context: Qt.ApplicationShortcut
        onActivated: controller.toggle_git_history()
    }

    // Jump to the FOCUSED agent's browser — go watch what the agent is doing
    // (or see the page it left once idle; the window lives as long as the
    // agent does). The keyboard twin of clicking an agent chip's globe. The
    // browser is agent-owned: there is no standalone browser tab anymore, so
    // you reach it only through the agent that owns it. 'B' still names the
    // browser, and the shape holds: already on the browser surface → BACK to
    // the surface you came from (usually the owning agent, not hard-coded
    // terminal); otherwise jump to the focused agent's newest-owned
    // window (clearing its attention dot). If the focused agent owns no window,
    // it's a no-op (agent-only reachability — see jump_to_focused_agent_browser).
    // The pool is QtWebEngine views embedded so they never escape into a
    // separate Hyprland window. See qml/BrowserSurface.qml.
    Shortcut {
        sequences: ["Ctrl+Shift+B"]
        context: Qt.ApplicationShortcut
        onActivated: controller.jump_to_focused_agent_browser()
    }

    // New browser window — the standard browser "new tab" idiom, gated to the
    // browser surface so nvim/terminal keep Ctrl+T elsewhere. The new (blank)
    // window's address bar is focused (BrowserSurface.focusActive), so the
    // keyboard loop is: Ctrl+T → type a URL → Enter. This is what makes the
    // browser surface operable without a mouse (keyboard-first, non-neg #1).
    Shortcut {
        sequences: ["Ctrl+T"]
        context: Qt.ApplicationShortcut
        enabled: controller.browserSurfaceVisible
        onActivated: controller.open_browser()
    }

    // IDE-wide file-manager toggle. Promoted out of the nvim layer
    // (previously `<leader>e` / `<C-u>` via `runtime/init.lua`'s hijack)
    // so the FM opens uniformly from any pane — editor, agent, terminal,
    // tree sidebar — without depending on nvim having focus. Same
    // ApplicationShortcut + Qt.ApplicationShortcut pattern as the swap
    // chords above. Opens at the anchored project root (or cached cwd when
    // unanchored), the same path the file-tree and git panes use.
    //
    // Trade-off: Qt.ApplicationShortcut intercepts before the editor terminal's key handling,
    // so nvim's built-in `<C-e>` (scroll viewport down one line) is silently
    // consumed when the editor pane is focused. Same precedence as Ctrl+Shift+T
    // and Ctrl+Shift+E (see CLAUDE.md swap-chords entry). Accepted: FM-from-any-
    // pane uniformity is worth the nvim scroll key loss for this project.
    Shortcut {
        sequences: ["Ctrl+E"]
        context: Qt.ApplicationShortcut
        onActivated: controller.toggle_fm()
    }

    // Manual file-tree sidebar toggle. Flips the *user-intent* half of
    // `treeVisible` (AppController.toggle_tree); the responsive width gate
    // still ANDs on top, so when the window is below SIDEBAR_MIN_WINDOW_WIDTH
    // the auto-hide overrides this toggle and the sidebar stays hidden. That
    // precedence is intentional for now (a future settings panel may make it
    // configurable).
    //
    // Application-scope, same as the surface-toggle chords above: it fires
    // from any pane, including nvim insert mode. Trade-off (accepted): in a
    // terminal Ctrl+S is XOFF (flow-control freeze) and some nvim configs map
    // it to `:w` — intercepting here means neither reaches the editor pane.
    // Repurposing Ctrl+S for the sidebar toggle is the user's explicit choice.
    Shortcut {
        sequences: ["Ctrl+S"]
        context: Qt.ApplicationShortcut
        onActivated: controller.toggle_tree()
    }

    // Terminal-agent chord family — the IDE-native orchestrator runtime.
    //
    // HARD CUTOVER NOTE (2026-06-10): these chords previously RELAYED to
    // orchestrator.nvim's keymaps inside the embedded nvim
    // (`controller.send_editor_keys` → nvim_input — see commit 45a8faa for
    // the VT-encoding rationale that made the relay necessary). The
    // IDE-native agent surface now owns them outright: Ctrl+1..5 focuses
    // the IDE's own agent slots, and orchestrator.nvim's <C-1..5>/<C-S-q>
    // keymaps are intentionally unreachable from the IDE. Per the
    // keybind-layer doctrine
    // (`.claude/memory/project/meta/ide_owns_keybind_layer.md`).
    //
    // Always-enabled (no surface gate): focus_agent auto-switches the
    // central surface to "agent", so the chords double as surface
    // switchers from anywhere. Ctrl+N addresses the Nth chip in DISPLAY
    // order (controller.agentOrder — dense, compacting on close), not an
    // internal slot. A position with no agent opens the spawn menu —
    // Ctrl+2 with one agent running means "give me a second one"; the
    // new agent always appends as the next dense number.
    Instantiator {
        model: controller.maxAgentSlots
        delegate: Shortcut {
            required property int index
            sequences: ["Ctrl+" + (index + 1)]
            context: Qt.ApplicationShortcut
            onActivated: {
                // Modal guard: opening the spawn menu (or yanking focus
                // via focus_agent) over the session picker would stack
                // two z-40 modals fighting for the key catcher.
                if (agentSessionPicker.visible)
                    return;
                var order = controller.agentOrder;
                if (index < order.length)
                    controller.focus_agent(order[index]);
                else
                    agentSpawnMenu.open();
            }
        }
    }

    // Agent chord — A-for-agent, the namespace entry point with
    // go-then-spawn semantics: from a non-agent surface it jumps to the
    // last-focused agent (focusedAgent survives surface swaps, so this
    // is a true "back to where I was"); pressed again while already on
    // the agent surface — or with an empty pool (focusedAgent == 0) —
    // it opens the spawn menu, the keyboard-first popup where n/c/r
    // spawn new/continue/resume (dangerous; Shift+letter = the
    // permission-checked variant) in the selected harness — `o` toggles
    // Claude ↔ OpenCode without burning more chords.
    Shortcut {
        sequences: ["Ctrl+Shift+A"]
        context: Qt.ApplicationShortcut
        onActivated: {
            // Modal guard (same rationale as _restoreCentralFocus): with
            // the menu already open — possibly over a NON-agent surface,
            // via Ctrl+N-on-empty — the go branch would focus_agent and
            // yank focus out of the modal, leaving it visible but deaf.
            // open() is idempotent and re-asserts the key catcher.
            if (agentSessionPicker.visible)
                return; // same modal guard as the Ctrl+N chords
            if (agentSpawnMenu.visible
                    || controller.focusedAgent === 0
                    || controller.agentSurfaceVisible)
                agentSpawnMenu.open();
            else
                controller.focus_agent(controller.focusedAgent);
        }
    }

    // MCP toggles popup — M-for-MCP. Opens the keyboard-first modal where a
    // single key flips each per-project MCP server for THIS project's agents
    // (`w` = chrome devtools / the embedded-browser capability). The toggle
    // lives in the popup (not a persistent top-bar affordance) to keep the
    // AgentTopBar clean. Modal guard: don't stack over another z-40 modal —
    // two self-healing key catchers would ping-pong focus (see
    // _restoreCentralFocus). open() is idempotent + re-asserts the catcher.
    Shortcut {
        sequences: ["Ctrl+Shift+M"]
        context: Qt.ApplicationShortcut
        onActivated: {
            if (agentSessionPicker.visible
                    || agentSpawnMenu.visible
                    || closeConfirmDialog.visible)
                return;
            mcpMenu.open();
        }
    }

    // Reload-in-place (Ctrl+Shift+R): save the session, quit, re-exec the
    // (possibly updated) binary, and auto-restore — pick up a new build
    // without losing the workspace. Routes through the teardown funnel, so
    // unsaved buffers prompt the 3-way dialog first; a clean tree reloads
    // straight away. Modal guard like the other popups.
    Shortcut {
        sequences: ["Ctrl+Shift+R"]
        context: Qt.ApplicationShortcut
        onActivated: {
            if (agentSessionPicker.visible || agentSpawnMenu.visible
                    || mcpMenu.visible || sessionsMenu.visible
                    || closeConfirmDialog.visible || unsavedChangesDialog.visible)
                return;
            controller.request_teardown(true);
        }
    }

    // Saved-session view (Ctrl+Shift+S): see / restore the saved session for
    // this project (the agents that were running + their titles).
    Shortcut {
        sequences: ["Ctrl+Shift+S"]
        context: Qt.ApplicationShortcut
        onActivated: {
            if (agentSessionPicker.visible || agentSpawnMenu.visible
                    || mcpMenu.visible || sessionsMenu.visible
                    || closeConfirmDialog.visible || unsavedChangesDialog.visible)
                return;
            sessionsMenu.open();
        }
    }

    // Close the focused slot. Shared between the agent and browser pools —
    // whichever surface is visible owns the chord (the two are never visible
    // together, so there is no ambiguity).
    Shortcut {
        sequences: ["Ctrl+Shift+Q"]
        context: Qt.ApplicationShortcut
        enabled: controller.agentSurfaceVisible || controller.browserSurfaceVisible
        onActivated: {
            if (controller.browserSurfaceVisible)
                controller.close_focused_browser();
            else
                controller.close_focused_agent();
        }
    }

    // Cycle through occupied slots without reaching for the digit row —
    // agent slots on the agent surface, browser windows on the browser
    // surface (same dispatch-by-visible-surface pattern as Ctrl+Shift+Q).
    Shortcut {
        sequences: ["Ctrl+Shift+H"]
        context: Qt.ApplicationShortcut
        enabled: controller.agentSurfaceVisible || controller.browserSurfaceVisible
        onActivated: {
            if (controller.browserSurfaceVisible)
                controller.cycle_browser_focus(-1);
            else
                controller.cycle_agent_focus(-1);
        }
    }
    Shortcut {
        sequences: ["Ctrl+Shift+L"]
        context: Qt.ApplicationShortcut
        enabled: controller.agentSurfaceVisible || controller.browserSurfaceVisible
        onActivated: {
            if (controller.browserSurfaceVisible)
                controller.cycle_browser_focus(1);
            else
                controller.cycle_agent_focus(1);
        }
    }

    // ~Half-page scroll of the focused agent terminal — Ctrl+U/D mirror vim's
    // half-page motion via agentSurface.scrollFocusedAgent, which calls the
    // fork's scrollPageFraction slot (NOT simulateWheel — that routes through
    // wheelEvent and would couple this to the physical mouse wheel). For a
    // mouse-tracking TUI like claude the fork emits scroll mouse-events to the
    // program rather than touching Konsole scrollback; claude's per-event line
    // multiplier is why the fraction is ~0.167, not 0.5 (see scrollFocusedAgent
    // for the tuning rationale). Gated off while the sidebar holds focus — the
    // tree owns Ctrl+U/D for its own paging.
    Shortcut {
        sequences: ["Ctrl+U"]
        context: Qt.ApplicationShortcut
        enabled: controller.agentSurfaceVisible && !treeScope.activeFocus
        onActivated: agentSurface.scrollFocusedAgent(1)
    }
    Shortcut {
        sequences: ["Ctrl+D"]
        context: Qt.ApplicationShortcut
        enabled: controller.agentSurfaceVisible && !treeScope.activeFocus
        onActivated: agentSurface.scrollFocusedAgent(-1)
    }

    // Clipboard paste into the terminal panes. Same VT-encoding gap as the
    // relay chords above: the fork's legacy key encoding collapses
    // Ctrl+Shift+V to Ctrl+V (0x1f mask strips Shift), so the widget never
    // receives a distinguishable paste chord — in stock Konsole this
    // shortcut lives in the embedding app, which here is us. We call the
    // widget's pasteClipboard() slot directly (Konsole's emitSelection:
    // bracketed paste, CRLF→CR normalization), targeting whichever central
    // terminal surface is visible. Editor pane: bracketed paste is what
    // nvim's TUI and orchestrator.nvim's embedded Claude Code terminals
    // expect, so text lands as a paste, not replayed keystrokes. Other
    // surfaces (agent/FM): disabled — Qt text fields handle Ctrl+V natively.
    Shortcut {
        sequences: ["Ctrl+Shift+V"]
        context: Qt.ApplicationShortcut
        enabled: controller.editorVisible || controller.terminalVisible || controller.agentSurfaceVisible
        onActivated: {
            if (controller.editorVisible) {
                editor.pasteClipboard();
            } else if (controller.agentSurfaceVisible) {
                // Bracketed paste into the focused agent's claude TUI —
                // the primary "paste a prompt" path on the agent surface.
                var term = agentSurface.focusedTerminal();
                if (term)
                    term.pasteClipboard();
            } else {
                terminalView.pasteClipboard();
            }
        }
    }

    // Clipboard copy from the terminal panes. Same VT-encoding gap as the
    // paste chord above: the fork's legacy key encoding collapses
    // Ctrl+Shift+C to Ctrl+C, so the widget would deliver SIGINT instead of
    // a copy chord. We call the widget's copyClipboard() slot directly
    // (no-op when nothing is selected — deliberately NOT falling through to
    // Ctrl+C, matching stock terminal-emulator behavior where the copy
    // chord never interrupts). Mouse selection over a mouse-reporting app
    // (nvim, claude TUI) requires Shift+drag per Konsole convention; the
    // selection this copies is the widget-level one either way.
    Shortcut {
        sequences: ["Ctrl+Shift+C"]
        context: Qt.ApplicationShortcut
        enabled: controller.editorVisible || controller.terminalVisible || controller.agentSurfaceVisible
        onActivated: {
            if (controller.editorVisible) {
                editor.copyClipboard();
            } else if (controller.agentSurfaceVisible) {
                var term = agentSurface.focusedTerminal();
                if (term)
                    term.copyClipboard();
            } else {
                terminalView.copyClipboard();
            }
        }
    }

    // Shift+Enter → newline: handled by the TERMINAL now, NOT an IDE shortcut.
    //
    // REGRESSION GUARD — do NOT re-add a Shift+Return ApplicationShortcut here.
    // There used to be one that injected "\\\r" (backslash + CR, claude's
    // line-continuation sequence) because the qmltermwidget fork's legacy key
    // encoding had no representation for Shift+Enter (no kitty CSI-u). That
    // workaround was the bug behind the literal "\" appearing in OpenCode (and
    // it could only ever produce a backslash, never a real newline): being an
    // ApplicationShortcut it consumed Shift+Enter BEFORE the QMLTermWidget, so
    // the terminal's own encoding never ran. The fork now implements the Kitty
    // keyboard protocol (MODIFICATIONS.md #13): it answers the "CSI ? u" probe,
    // so OpenCode/Claude negotiate kitty and the widget reports Shift+Enter as
    // ESC[13;2u — which both decode as "insert newline" natively, with no stray
    // backslash. Removing this interception is what lets that reach the apps.
    // (Editor surface was already excluded — nvim owns its Shift+Enter; bare
    // zsh, which doesn't negotiate kitty, now gets the keytab's legacy
    // Return+Shift entry, \EOM — harmless, just no newline-on-Shift+Enter.)

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
    // widget's keyPressEvent, exactly like Ctrl+Shift+P — see the
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
            else if (controller.agentSurfaceVisible)
                // Terminal-agent surface: re-request focus for the focused
                // slot — the delegate's onFocusAgentRequested handler lands
                // it on the visible agent terminal.
                controller.focus_agent(controller.focusedAgent);
            else if (controller.browserSurfaceVisible)
                // Embedded browser surface: focus_browser re-emits
                // focusBrowserRequested → BrowserSurface.focusActive() lands
                // focus on the page (or address bar for a blank window).
                // No-ops on an empty pool. Without this branch the fall-through
                // would focus the hidden editor.
                controller.focus_browser(controller.focusedBrowser);
            else if (controller.gitVisible)
                gitHistoryView.focusContent();
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
    // ALSO `gitStatusPanel.reachable` (visible && !collapsed). When the side panel doesn't have
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
    // gitStatusPanel.reachable gate on Ctrl+K is important: when the
    // working tree is clean the changes pane is `visible: false`, and
    // while the git "changes" surface is up it is `collapsed` (visible
    // but folded to 0 height) — in either case its inner items can't
    // accept focus. `reachable` (visible && !collapsed) covers both.
    // Without the gate, Ctrl+K would land focus on an unfocusable item —
    // focus would silently disappear and the user couldn't navigate
    // anywhere with keys until they clicked or pressed Ctrl+H back to a
    // central pane.
    Shortcut {
        sequences: ["Ctrl+K"]
        context: Qt.ApplicationShortcut
        enabled: treeScope.activeFocus && gitStatusPanel.reachable
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

    // IDE-wide fuzzy file finder. ApplicationShortcut so it fires from
    // EVERY surface — editor, terminal, agent, FM, tree — replacing
    // fff.nvim's in-editor Ctrl+F with the native overlay (same Rust
    // fff engine; fff.nvim keeps working in standalone nvim, where this
    // chord layer doesn't exist). Cost, same shape as the Ctrl+H/L
    // tradeoff above: terminal Ctrl+F (readline forward-char) and
    // nvim's Ctrl+F (page-forward / fff.nvim) are unreachable inside
    // the IDE — accepted per the IDE-owns-the-keybind-layer doctrine.
    Shortcut {
        sequences: ["Ctrl+F"]
        context: Qt.ApplicationShortcut
        onActivated: {
            const ws = treeOpsWindowState;
            // Toggle when already open; refuse to stomp a DIFFERENT
            // in-flight modal (rename/create/delete hold typed input —
            // requestFuzzyFinder would discard it unconditionally).
            if (ws.activeModal === ws.modalFuzzyFinder)
                ws.closeModal();
            else if (ws.activeModal === ws.modalNone)
                ws.requestFuzzyFinder();
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
        // Outer RowLayout: `mainContent` (the editor | AgentPane
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

                // Editor surface: a QMLTermWidget (our fork of Konsole's VT
                // engine, see /home/jc/projects/symmetria-qmltermwidget) hosting
                // `nvim --listen` as a TUI. nvim renders its grid here via the
                // C++-fast terminal; the IDE chrome (cmdline/which-key/minimap/
                // status) is fed by rpcnotify relays over the RPC socket the
                // Python NvimBackend attaches to — independent of this widget.
                // The QMLTermSession spawns nvim itself (KSession), using the
                // editorProgram/editorArgs context props built in app.py.
                QMLTermWidget {
                    id: editor
                    anchors.fill: parent
                    // The minimap (declared as a sibling below) occupies a
                    // fixed-width ribbon on mainContent's right edge when it is
                    // visible; reserving that strip via rightMargin keeps the
                    // editor's visible boundary correct. Currently gated off
                    // (root.minimapEnabled === false), so this resolves to 0.
                    anchors.rightMargin: minimap.visible ? minimap.width : 0
                    // editor is ONE of the central surfaces (terminal / agent /
                    // FM are the others). Visible only when the central surface
                    // is the editor AND no full-slot pane (agent/FM) is active.
                    // The XOR is guaranteed at the AppController layer.
                    visible: !controller.agentVisible && !controller.fmVisible && controller.editorVisible
                    focus: visible

                    // Transparency: useFBORendering MUST be false (the FBO path
                    // has no alpha → opaque); the image path is still C++-fast.
                    // colorScheme "Symmetria" applies our 0.6 background opacity
                    // (the fork's parse+apply fix). fillColor transparent + the
                    // Window's transparent clear = wallpaper blend on Hyprland.
                    // Retune live via the `backgroundOpacity` Q_PROPERTY.
                    colorScheme: "Symmetria"
                    useFBORendering: false
                    fillColor: "transparent"
                    blinkingCursor: true
                    // Padding between pane edge and character grid — the
                    // fork's `margin` Q_PROPERTY; the translucent background
                    // still covers the padded strip.
                    margin: Theme.size.terminalPadding
                    // Copy-on-select (fork modification #11): a completed
                    // mouse selection (drag release / double-click word /
                    // triple-click line) lands on the clipboard without a
                    // chord; Ctrl+Shift+C stays as the explicit re-copy.
                    autoCopySelectedText: true
                    // Same font the IDE chrome overlays bind to (editor_font.py).
                    font.family: editorFontFamily
                    font.pointSize: editorFontPointSize
                    // Per-glyph fallback chain (fork modification #9). QML
                    // font values are single-family (gotcha #23), so the
                    // editor_font.py cascade cannot ride font.family —
                    // without this, missing glyphs (✻ spark, dingbats)
                    // resolve via Qt's generic fallback, not Ghostty's.
                    fallbackFamilies: editorFontFallbacks
                    // FULL hinting + the v35 FreeType interpreter set in
                    // app.py::run (FREETYPE_PROPERTIES) — chosen by a 3-way
                    // device-res screenshot comparison (2026-06-09):
                    //   - NO hinting: even but visibly soft.
                    //   - VERTICAL hinting: baselines crisp, but vertical
                    //     stems still smear across device columns.
                    //   - FULL hinting (+v35 for true horizontal stem
                    //     snapping): Ghostty-class sharpness.
                    // Full hinting WAS the original uneven-rendering bug —
                    // but only because run glyphs drifted off the cell grid
                    // at the font's natural fractional advance. The fork's
                    // letter-spacing cell snap (TerminalDisplay::fontChange)
                    // fixed the drift, which makes full hinting safe: worst
                    // case is ±0.5px per-glyph blit rounding, no
                    // accumulation. Must be set HERE: the QML font value is
                    // built fresh from the family/pointSize context props,
                    // so editor_font.py's QFont preference does not carry
                    // through. Keep both sites in sync.
                    font.hintingPreference: Font.PreferFullHinting

                    session: QMLTermSession {
                        id: editorSession
                        shellProgram: editorProgram
                        shellProgramArgs: editorArgs
                        // `editorRoot`, NOT `displayedRoot`: nvim's cwd is held
                        // off $HOME (fff.nvim recursive-watch thermal bug). The
                        // shell pane below deliberately keeps `displayedRoot` so
                        // it starts at $HOME and `workspace_autocd` fires.
                        initialWorkingDirectory: controller.editorRoot
                        // nvim exiting (`:qa`) tears down the IDE — same
                        // semantics as the `backend.closed` wire, which also
                        // fires when the RPC socket drops. Route through the
                        // quit choke point (NOT Qt.quit) so the window-close
                        // guard treats it as authorized and does NOT prompt
                        // the confirm dialog over an already-dead editor.
                        onFinished: controller.authorize_and_quit()
                    }

                    // Spawn nvim once the widget exists; grab focus if shown.
                    Component.onCompleted: {
                        editorSession.startShellProgram()
                        if (visible)
                            forceActiveFocus()
                    }
                    onVisibleChanged: if (visible)
                        forceActiveFocus()

                    // IDE chords (Ctrl+Shift+E/A/T) are Qt.ApplicationShortcut
                    // at the Window root and are expected to win over the
                    // terminal's own key handling (verified live). If a chord
                    // ever regresses, the fix is to re-expose TerminalDisplay's
                    // `overrideShortcutCheck(QKeyEvent*, bool&)` on the QML-facing
                    // qtermwidget in the fork and deny the override for our
                    // Ctrl+Shift chords — it is NOT currently reachable from QML.

                    // Floating cmdline + wildmenu overlay — parented to the
                    // editor so it clips within the viewport and tracks resizes.
                    // Focus stays on the terminal; keys flow to nvim. NeoVim
                    // relays the cmdline via vim.ui_attach over the RPC socket;
                    // this overlay reads it via cmdlineState / completionModel.
                    CommandLine {
                        id: cmdlineOverlay
                        anchors.fill: parent
                    }

                    // Native which-key overlay. Bottom-anchored inside the
                    // editor so it sits above the status bar. Driven entirely by
                    // `whichKeyState` + `whichKeyModel`; the Lua side controls
                    // show/hide via rpcnotify (runtime/lua/orchestrator/whichkey).
                    WhichKeyOverlay {
                        id: whichKeyOverlay
                        anchors.left: editor.left
                        anchors.right: editor.right
                        anchors.bottom: editor.bottom
                        height: Math.min(implicitHeight, editor.height * 0.5)
                        z: 20
                    }
                }

                // Phase 2.5 terminal pane — sibling of the editor under
                // `mainContent`, now also a QMLTermWidget (forked Konsole VT
                // engine). Both panes fill the same slot and are gated on a
                // `visible` binding; mutual exclusivity is guaranteed at the
                // AppController layer (`centralSurface` is a single string, the
                // derived booleans are XOR by construction). The shell is
                // spawned by the QMLTermSession in Component.onCompleted.
                //
                // cwd sync: the QMLTermSession exposes `currentDir` natively
                // (Konsole reads the foreground process cwd via /proc — no
                // shell-side OSC 7 hook). A Timer polls it and calls
                // `controller.on_shell_cwd` only on change, which routes through
                // the same `_route_capsule({id:"cwd"})` path nvim's `:cd` uses.
                //
                // Focus: `focus: visible` + Component.onCompleted/onVisibleChanged
                // forceActiveFocus so the Ctrl+Shift+E/T chords land typing-ready.
                QMLTermWidget {
                    id: terminalView
                    anchors.fill: parent
                    visible: !controller.agentVisible && !controller.fmVisible && controller.terminalVisible
                    focus: visible

                    colorScheme: "Symmetria"
                    useFBORendering: false
                    fillColor: "transparent"
                    blinkingCursor: true
                    // Same padding + hinting + copy-on-select rationale as
                    // the editor pane above — the panes share one contract.
                    margin: Theme.size.terminalPadding
                    autoCopySelectedText: true
                    font.family: editorFontFamily
                    font.pointSize: editorFontPointSize
                    fallbackFamilies: editorFontFallbacks
                    font.hintingPreference: Font.PreferFullHinting

                    session: QMLTermSession {
                        id: shellSession
                        shellProgram: shellExec
                        shellProgramArgs: shellExecArgs
                        initialWorkingDirectory: controller.displayedRoot
                        // A shell exiting (user typed `exit`) leaves the pane
                        // inert in v1 — log-only parity with the old
                        // `_on_terminal_closed`. Respawn is a follow-up.
                        onFinished: console.log("shell session finished")
                    }

                    // Poll the shell cwd. KSession has no currentDirChanged
                    // signal, so we sample on a 600ms cadence and forward only
                    // on change — the coalesced replacement for per-keystroke
                    // OSC 7. 600ms is imperceptible for a file-tree follow and
                    // cheap (one /proc read). Gated on `terminalView.visible`:
                    // the shell only changes cwd when focused (the user types
                    // `cd`), so there's nothing to poll while the editor/agent
                    // is the active surface. `_lastShellDir` persists across the
                    // pause, so the first `cd` after re-entry still syncs.
                    property string _lastShellDir: ""
                    Timer {
                        interval: 600
                        repeat: true
                        running: terminalView.visible
                        onTriggered: {
                            var d = shellSession.currentDir
                            if (d && d !== terminalView._lastShellDir) {
                                terminalView._lastShellDir = d
                                controller.on_shell_cwd(d)
                            }
                        }
                    }

                    Component.onCompleted: {
                        shellSession.startShellProgram()
                        if (visible)
                            forceActiveFocus()
                    }
                    onVisibleChanged: if (visible)
                        forceActiveFocus()
                }

                // IDE-native orchestrator surface — the terminal-agent pool.
                // Claude CLI instances run in their own QMLTermWidget panes
                // (one per pool slot), replacing the orchestrator.nvim
                // terminal-buffer workflow inside the embedded nvim. One
                // agent is visible at a time (`controller.focusedAgent`);
                // Ctrl+1..5 / Ctrl+Shift+H/L switch which.
                //
                // STRUCTURE IS LOAD-BEARING: a fixed `model:
                // controller.maxAgentSlots` Repeater over per-slot Loaders.
                // Spawning = the slot's `agentSlotActive[index]` flips true →
                // Loader instantiates the terminal and starts claude. Closing
                // = the flag flips false → Loader teardown destroys the
                // KSession, whose destructor hangs up the Pty and reaps the
                // claude child. Do NOT replace the fixed Repeater with a
                // model over `activeAgentSlots` — list churn would
                // destroy/recreate sibling delegates and kill live claude
                // processes on every spawn/close.
                Item {
                    id: agentSurface
                    anchors.fill: parent
                    visible: controller.agentSurfaceVisible && !controller.fmVisible && !controller.agentVisible

                    // Empty-state affordance: the surface is reachable with
                    // no agents alive (AgentTopBar switcher click), and an
                    // unexplained empty pane reads as broken. Names the
                    // spawn chord so the path is discoverable without docs.
                    Column {
                        anchors.centerIn: parent
                        spacing: Theme.spacing.sm
                        visible: controller.focusedAgent === 0

                        Text {
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: "no agents running"
                            color: Theme.color.text.dim
                            font.family: Theme.font.family
                            font.pixelSize: Theme.font.size.sm
                            renderType: Text.NativeRendering
                        }
                        Text {
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: "Ctrl+Shift+A — spawn an agent"
                            color: Theme.color.text.normal
                            font.family: Theme.font.family
                            font.pixelSize: Theme.font.size.sm
                            renderType: Text.NativeRendering
                        }
                    }

                    /// The focused slot's live QMLTermWidget, or null.
                    function focusedTerminal() {
                        var idx = controller.focusedAgent - 1;
                        if (idx < 0)
                            return null;
                        var loader = agentSlotRepeater.itemAt(idx);
                        return (loader && loader.item) ? loader.item : null;
                    }

                    // Bridge-mediated STT injection (controller →
                    // agentInjectRequested → here). Python validated the
                    // slot but cannot touch the KSession (not wrappable);
                    // delivery happens here: a bracketed paste into the
                    // slot's pty — claude's TUI enables bracketed paste,
                    // so embedded newlines arrive as paste content, not
                    // premature submits — then, for submit requests, Enter
                    // after a short settle delay (mirrors orchestrator.
                    // nvim's stt_inject, which waits for the terminal
                    // buffer to absorb the paste before sending Enter).
                    // One in-flight request at a time: STT deliveries are
                    // serial by construction (one recording at a time), so
                    // a second concurrent request reports busy rather than
                    // racing the settle timer.
                    property var _injectPending: null

                    Timer {
                        id: injectSubmitTimer
                        // WORKAROUND: 150ms settle guess before Enter — the
                        // fork's KSession exposes no paste-absorbed signal,
                        // so there is nothing to acknowledge against
                        // (orchestrator.nvim waits on terminal-buffer
                        // onLines; a raw pty has no equivalent). Remove if
                        // the fork ever surfaces a bracketed-paste-complete
                        // signal on the QML-facing session.
                        interval: 150
                        onTriggered: {
                            var p = agentSurface._injectPending;
                            agentSurface._injectPending = null;
                            if (!p)
                                return;
                            var loader = agentSlotRepeater.itemAt(p.slot - 1);
                            // Identity check, not just liveness: if the agent
                            // exited during the settle window and a NEW agent
                            // spawned into the freed slot (slots fill from
                            // the bottom), the loader holds a different item
                            // — Enter there would auto-submit into the wrong
                            // pane. A destroyed item never compares equal to
                            // its replacement, so this catches close AND
                            // slot reuse.
                            if (loader && loader.item && loader.item === p.term
                                    && loader.item.session) {
                                loader.item.session.sendText("\r");
                                controller.agent_inject_done(p.requestId, true, true, "");
                            } else {
                                controller.agent_inject_done(p.requestId, false, false, "agent-closed");
                            }
                        }
                    }

                    // Deliberately NOT gated on agentSurface.visible: the
                    // target is the slot's pty, not the foreground surface —
                    // STT must deliver while the user is looking at the
                    // editor/terminal/FM. Do not add a visibility guard.
                    Connections {
                        target: controller
                        function onAgentInjectRequested(slot, text, submit, requestId) {
                            // Python already validated `slot in _term_agents`;
                            // this re-check is the Loader-liveness backstop
                            // (the bookkeeping dict and the Loader item can
                            // briefly disagree around spawn/teardown). The
                            // .session guard keeps a mid-construction item
                            // from throwing past agent_inject_done — the
                            // requester would otherwise wait out the bridge's
                            // full timeout instead of failing fast.
                            var loader = agentSlotRepeater.itemAt(slot - 1);
                            if (!loader || !loader.item || !loader.item.session) {
                                controller.agent_inject_done(requestId, false, false, "no-pane");
                                return;
                            }
                            if (agentSurface._injectPending) {
                                controller.agent_inject_done(requestId, false, false, "busy");
                                return;
                            }
                            loader.item.session.sendText("\x1b[200~" + text + "\x1b[201~");
                            if (submit) {
                                agentSurface._injectPending = ({ requestId: requestId, slot: slot, term: loader.item });
                                injectSubmitTimer.restart();
                            } else {
                                controller.agent_inject_done(requestId, true, false, "");
                            }
                        }
                    }

                    /// Half-page scroll of the focused agent terminal, bound to
                    /// Ctrl+U/D. direction: +1 = up (older output), -1 = down.
                    /// Calls the fork's dedicated `scrollPageFraction` slot —
                    /// DELIBERATELY NOT `simulateWheel`: simulateWheel routes
                    /// through the C++ wheelEvent, which would couple this to the
                    /// physical mouse wheel and change the wheel's sensitivity.
                    /// Keeping a separate slot lets the keybind jump half a page
                    /// while the mouse wheel keeps its stock per-notch feel.
                    function scrollFocusedAgent(direction) {
                        var term = focusedTerminal();
                        if (!term)
                            return;
                        // ~1/6 of visible height per press. Intentionally below a
                        // literal "half page": claude's TUI scrolls several of
                        // its own lines per wheel event scrollByLines emits, so a
                        // 0.5 fraction overshot to ~1.5 pages. 0.167 lands near a
                        // real half-page jump. Tune here if the feel drifts.
                        term.scrollPageFraction(0.167, direction);
                    }

                    Repeater {
                        id: agentSlotRepeater
                        model: controller.maxAgentSlots

                        delegate: Loader {
                            id: slotLoader

                            required property int index
                            readonly property int slot: slotLoader.index + 1

                            anchors.fill: parent
                            active: controller.agentSlotActive[slotLoader.index]
                            visible: controller.focusedAgent === slotLoader.slot

                            sourceComponent: QMLTermWidget {
                                id: agentTerm

                                // Same rendering contract as the editor +
                                // shell panes above (transparency invariants,
                                // padding, font cascade + hinting) — keep the
                                // three in sync.
                                colorScheme: "Symmetria"
                                useFBORendering: false
                                fillColor: "transparent"
                                blinkingCursor: true
                                margin: Theme.size.terminalPadding
                                autoCopySelectedText: true
                                font.family: editorFontFamily
                                font.pointSize: editorFontPointSize
                                fallbackFamilies: editorFontFallbacks
                                font.hintingPreference: Font.PreferFullHinting

                                session: QMLTermSession {
                                    id: agentSession
                                    initialWorkingDirectory: controller.displayedRoot
                                    // claude exited on its own (/exit or crash)
                                    // — Python drops the slot, which flips the
                                    // Loader off. Idempotent with an explicit
                                    // close (the slot is already gone then).
                                    onFinished: controller.on_agent_finished(slotLoader.slot)
                                    // OSC 0/2 terminal title from claude — the
                                    // v1 session-title source for the top-bar
                                    // chips (Python debounces the churn).
                                    onTitleChanged: controller.on_agent_title(slotLoader.slot, agentSession.title)
                                }

                                Component.onCompleted: {
                                    // One-shot argv read (gotcha #3 doesn't
                                    // apply — deliberately not a binding; the
                                    // spawn spec is frozen at load time).
                                    // env-wrapper argv because KSession's
                                    // setEnvironment is not QML-reachable.
                                    var argv = controller.agent_spawn_argv(slotLoader.slot)
                                    if (argv.length > 0) {
                                        agentSession.shellProgram = argv[0]
                                        // Manual copy, not argv.slice(1):
                                        // QVariantList from a PySide slot is
                                        // not a real JS Array in Qt 6.11 (see
                                        // memory: Array.isArray rejects it).
                                        var args = []
                                        for (var i = 1; i < argv.length; i++)
                                            args.push(argv[i])
                                        agentSession.shellProgramArgs = args
                                        agentSession.startShellProgram()
                                    }
                                    if (visible)
                                        forceActiveFocus()
                                }
                                onVisibleChanged: if (visible)
                                    forceActiveFocus()

                                // Focus pull for the already-visible case
                                // (chord pressed while the sidebar held focus
                                // and this slot was already showing) — the
                                // onVisibleChanged path above only covers
                                // slot/surface switches.
                                Connections {
                                    target: controller
                                    function onFocusAgentRequested(slot) {
                                        if (slot === slotLoader.slot && agentTerm.visible)
                                            agentTerm.forceActiveFocus()
                                    }
                                }
                            }
                        }
                    }
                }

                // Embedded browser surface — sibling central surface (the
                // agentic-browser pool). QtWebEngine WebEngineViews embedded
                // so the browser renders into THIS window and never creates a
                // top-level Hyprland surface (containment by construction).
                // Gated on the same XOR as the other central surfaces;
                // `browserSurfaceVisible` is derived from
                // `centralSurface == "browser"`. Ctrl+Shift+B toggles it.
                // Deferred until the FIRST browser open (manual Ctrl+T or an
                // agent's browser_open flips controller.browserEverOpened).
                // Eager instantiation cost cold start ~430ms — the `import
                // QtWebEngine` QML module load + the persistent WebEngineProfile
                // creation — for a surface most launches never touch (measured
                // via SYMMETRIA_IDE_TRACE: engine.load ~696ms → ~268ms with this
                // stubbed). The inner view pool is already lazy (per-slot
                // Loaders), so this only defers the WebEngine SCAFFOLDING.
                // Activation is synchronous and the latch is set BEFORE the slot
                // becomes active (AppController.open_browser), so the surface +
                // its pool exist before focus_browser / the view delegate run.
                // NOTE: unlike the reverted AgentPane-Loader experiment (a
                // ~12-25ms component), this defers a ~430ms heavyweight — a
                // different risk/reward. Re-verified no tree-mount regression on
                // the bambin repo via bench/measure_mount.py.
                Loader {
                    id: browserSurfaceLoader
                    anchors.fill: parent
                    active: controller.browserEverOpened
                    visible: controller.browserSurfaceVisible
                        && !controller.fmVisible
                        && !controller.agentVisible
                    sourceComponent: BrowserSurface {
                        anchors.fill: parent
                    }
                }

                // Git-history viewer — sibling central surface (read-only
                // comprehension surface). Gated on the same XOR as the editor/
                // terminal/agent siblings; `gitVisible` is derived from
                // `centralSurface == "git"`. Injects the history controller +
                // model (context properties) rather than reaching for globals,
                // so the subtree extracts cleanly into a future
                // Symmetria.Git.UI module. Ctrl+Shift+G toggles it.
                GitHistoryView {
                    id: gitHistoryView
                    anchors.fill: parent
                    visible: !controller.agentVisible && !controller.fmVisible && controller.gitVisible
                    focus: visible
                    logController: gitLogController
                    logModel: gitLogModel
                    // Live working-tree file list for the "changes" sub-view —
                    // the same GitStatusListModel the Active Changes side panel
                    // renders, reused rather than re-scanned.
                    statusModel: gitStatusList
                    // FM file-tree inputs for the "changes" master pane — the
                    // SAME three the Active Changes side panel (gitStatusPanel)
                    // binds, so the central changes tree and the side mini-tree
                    // render the identical changeset from one source of truth.
                    repoRoot: gitController.repoRoot
                    statusProvider: gitProviderAdapter
                    pathFilter: gitController.changedPathSet
                    // On show, pick the default sub-view by worktree state —
                    // "changes" if dirty, else "history" (a clean tree's changes
                    // view is empty, so the committed log is the useful landing).
                    onVisibleChanged: if (visible) enterSurface()
                    // Activating a file row in the changes tree opens it in
                    // nvim and swaps to the editor — "I've read this diff, take
                    // me to edit it" in one keystroke. Same open path the side
                    // panel + main tree use.
                    onFileActivated: function (absolutePath) {
                        controller.open_in_nvim(absolutePath);
                        controller.set_central_surface("editor");
                    }
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
                    // Env-gated since the terminal-agent surface took over
                    // "agent" as a central surface: legacySdkPaneEnabled is
                    // true only for SYMMETRIA_IDE_SDK_PANE=1 or the SDK
                    // smoke-test env vars (AGENT_PROMPT / AGENT_VIEW).
                    visible: legacySdkPaneEnabled && controller.agentVisible && !controller.fmVisible
                }

                // Phase 0 editor minimap (docs/minimap-prd.md). Narrow
                // ribbon anchored to mainContent's right edge, only
                // visible when the editor is the active central surface
                // — same visibility gate as the editor, so the minimap and
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
                    // content mutation repaints the silhouette.
                    model: minimapModel
                    // Above the central panes so the ribbon visibly sits
                    // on top of the editor's right edge.
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

                // NOTE: there is deliberately NO active-pane focus border
                // around the central surface. A `mainContentFocusBorder`
                // Rectangle used to paint a 1px accent hairline
                // (Theme.color.accent.focus, white @ ~40% alpha) around the
                // FM and agent panes when they were active. It was removed
                // by design: focus-status borders belong only to the SIDE
                // panels (the per-sub-pane FocusBars below), never to the
                // central/main surface. Do not re-introduce a full-envelope
                // focus border here — the side-panel FocusBars are the sole
                // "where is focus" affordance, and a central envelope reads
                // as visual noise over the dominant full-bleed surfaces.

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

                    // Start picker mode when the panel first opens. We ride
                    // the panel's picker infrastructure (built for the XDG
                    // portal) for its open/cancel routing: confirming a
                    // selection emits FileManagerService.pickerCompleted
                    // (→ pick_in_nvim, opens in the editor + dismisses);
                    // cancelling emits pickerCancelled. We connect to both
                    // below — no fifoPath is passed, so the panel's
                    // standalone-host FIFO writer is dormant.
                    //
                    // `fileOps: true` makes this a FULL file manager rather
                    // than a bare file chooser: it opts out of picker mode's
                    // default clipboard/multi-select suppression
                    // (FileOpsHandler.js gates the suppression on
                    // !pickerFileOps), so yank/cut/paste, space-marking, and
                    // tabs all work — while Enter→open-in-nvim and Esc→dismiss
                    // stay wired through the picker signals. The flag is opt-in
                    // and defaults false, so the standalone FM and the XDG
                    // portal picker are unaffected (they never pass it).
                    onLoaded: {
                        FmUi.FileManagerService.startPickerMode({
                            title: "Open File",
                            acceptLabel: "Open",
                            fileOps: true
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
            // Visibility tracks `treeVisible`, so the pixel is reclaimed
            // cleanly whenever the sidebar hides — via the responsive
            // width gate or the Ctrl+S toggle (controller.toggle_tree).
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
            // Visibility is `controller.treeVisible` — visible by default
            // (the "visualization-first" decision), but now driven by the
            // responsive width gate (auto-hide below SIDEBAR_MIN_WINDOW_WIDTH)
            // and the manual Ctrl+S toggle (controller.toggle_tree).
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
                // FocusBars follow a color-flip contract (accent.focus ↔
                // transparent on color, never on geometry — so focus
                // transitions cost no layout round-trip) but render as a
                // left-edge bar rather than a full envelope — subtler, and
                // clear of the scrollbar. These side-panel bars are now the
                // ONLY focus-status affordance; the central surface has no
                // equivalent border by design (see the note above
                // fmPaneLoader).

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
                    // Ctrl+Shift+P updates this header without any extra
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
                        // Fold away while the git "changes" surface is on
                        // screen — that surface IS the full changes tree, so
                        // this side mini-tree would just duplicate it. Only in
                        // CHANGES mode, not history: when the central surface
                        // shows commits, the side changes tree is a useful
                        // at-a-glance reference again, so it slides back down.
                        collapsed: controller.centralSurface === "git"
                                   && gitHistoryView.mode === "changes"
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
                        // column. Geometry + rationale live in
                        // FocusBar.qml.
                        FocusBar {
                            focused: gitStatusPanel.activeFocus
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
                            // rootPath + restoreExpandedPaths are assigned
                            // TOGETHER, imperatively, by `_applyTreeMount()` —
                            // deliberately NOT two independent declarative
                            // bindings. With separate bindings (rootPath ←
                            // displayedRoot, restoreExpandedPaths ←
                            // expandedPathsCache) on the same controller, QML
                            // does not guarantee which one is applied first when
                            // displayedRoot changes. Under the THREADED render
                            // loop the rootPath update can reach the FM's
                            // onRootPathChanged → beginMount BEFORE the
                            // restoreExpandedPaths update is applied, so
                            // beginMount reads an empty set and falls through to
                            // lazyExpand — the whole tree mounts COLLAPSED.
                            // The bug was intermittent and only surfaced on real
                            // (threaded-loop) launches with a CLEAN working tree:
                            // when the tree is dirty the Active Changes panel
                            // mounts its own inner FileTreeView, and that extra
                            // instantiation work delays the main tree's rootPath
                            // update until after the cache update lands, masking
                            // the race. Assigning both in ONE synchronous
                            // function, restore-set FIRST, makes beginMount
                            // always observe the fresh set.
                            //
                            // `displayedRoot` (NOT raw `cwd`): the tree pins to
                            // the anchored project root when anchored; identical
                            // to `cwd` when not. Ctrl+Shift+P toggles the anchor
                            // (`:SymmetriaAnchor`/`:SymmetriaUnanchor` scripted).
                            //
                            // `rootPath` is a REQUIRED property on the FM's
                            // FileTreeView, so it must be initialized
                            // declaratively at the instantiation site (a purely
                            // imperative Component.onCompleted assignment is too
                            // late and fails component creation). Seed it empty —
                            // beginMount short-circuits on an empty rootPath, so
                            // no mount fires here — then `_applyTreeMount()` does
                            // the real ordered assignment below.
                            rootPath: ""
                            // Apply a mount from an EXPLICIT (root, expanded)
                            // pair. The expanded set is passed in — never read
                            // from `controller.expandedPathsCache` here — because
                            // that property and `displayedRoot` are updated by
                            // different connections with no guaranteed ordering:
                            // reading the cache out-of-band at mount time yields
                            // the PREVIOUS root's set on cd-driven remounts, whose
                            // paths all fail the new root's prefix filter, so the
                            // restore queue builds empty and the tree collapses to
                            // root-only. The controller's `treeMountRequested`
                            // signal delivers root + its freshly-loaded cache
                            // together, so the pair is consistent by construction.
                            function _applyTreeMount(root, expanded) {
                                // Unchanged root => nothing to remount. The
                                // synthetic startup emit (seeded for GitController)
                                // re-runs this with the same root right after
                                // Component.onCompleted already mounted it; the
                                // guard keeps it a clean single mount.
                                // ASSUMPTION (safe for all current callers): a
                                // same-root emit always carries an IDENTICAL
                                // `expanded` set, because `_sync_expanded_paths_cache`
                                // only reloads on `displayedRootChanged` (root
                                // always differs across real switches). If a future
                                // path ever emits treeMountRequested for the same
                                // root with a DIFFERENT cache (a "reload tree"
                                // command, external cache refresh), this guard would
                                // silently drop the new set — it must then also
                                // compare `expanded` against the last-applied set.
                                if (root === fileTreeView.rootPath)
                                    return;
                                // Order is load-bearing: restore set BEFORE root
                                // so the FM's beginMount (fired synchronously by
                                // the rootPath write below) reads the right list.
                                fileTreeView.restoreExpandedPaths = expanded;
                                fileTreeView.rootPath = root;
                            }
                            // Initial mount (engine.load): the cache was already
                            // populated in AppController.__init__ before the QML
                            // loaded AND no `treeMountRequested` has fired yet
                            // (this Connections didn't exist when __init__ called
                            // `_sync_expanded_paths_cache`), so pull the current
                            // values directly — they're consistent at startup.
                            // The later synthetic startup emit re-fires this with
                            // the SAME root (the unchanged-root guard absorbs it)
                            // ONLY because `displayedRoot` is identical between now
                            // and `start()`'s emit; if `start()` ever mutated the
                            // root before that emit, the guard would (correctly)
                            // permit a second mount.
                            Component.onCompleted: fileTreeView._applyTreeMount(
                                controller.displayedRoot, controller.expandedPathsCache)
                            // Every later root change (cwd move, anchor toggle,
                            // the synthetic startup emit) arrives via
                            // treeMountRequested, which carries the root and its
                            // freshly-loaded cache as one atomic payload — no
                            // stale-property read possible.
                            Connections {
                                target: controller
                                function onTreeMountRequested(root, expanded) {
                                    fileTreeView._applyTreeMount(root, expanded);
                                }
                            }
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
                            // Per-project expanded-state cache (option 6).
                            // restoreExpandedPaths is assigned imperatively in
                            // `_applyTreeMount()` above (NOT bound here) — see
                            // that function for why the declarative-binding form
                            // raced the rootPath binding and collapsed the tree
                            // on clean-working-tree mounts. The saved set comes
                            // from `tree_state_cache.py` via AppController's
                            // `_sync_expanded_paths_cache`; empty list (no cache
                            // yet) => the FM falls through to `lazyExpand`,
                            // non-empty => the FM restores the saved tree shape.
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
                            // File operations (d/r/a delete/rename/create,
                            // y/x/p yank/cut/paste, Space multi-select,
                            // g/c/, chords). The FM's shared FileOpsHandler
                            // only dispatches when a non-null WindowState is
                            // bound — without it the tree is navigation-only
                            // (its pre-2026-06 IDE behavior). The instance +
                            // the modal popups it drives live as treeScope
                            // siblings below (see the "File-operation state"
                            // block after the ColumnLayout).
                            windowState: treeOpsWindowState
                            onFileActivated: function (path) {
                                controller.open_in_nvim(path);
                                if (editor.visible)
                                    editor.forceActiveFocus();
                            }
                        }

                        // Per-sub-pane focus indicator for the main
                        // FileTreeView. Symmetric counterpart of
                        // GitStatusPanel's focus bar above. Bound
                        // to `mainTreeScope.activeFocus` (the wrapping
                        // FocusScope), which flips when the inner
                        // ListView gains/loses activeFocus.
                        FocusBar {
                            focused: mainTreeScope.activeFocus
                        }
                    }
                }

                // === File-operation state + modal surfaces (main tree) ===
                //
                // The FM keeps FileTreeView popup-free on purpose: in the
                // standalone FM the modal layer lives once at
                // FileManager.qml scope and is shared by both views. The
                // IDE replicates that host role here, scoped to the side
                // panel (popups overlay treeScope, not the whole window).
                //
                // Every modal reachable from tree keys MUST be hosted:
                // while `activeModal != modalNone` the tree swallows ALL
                // keys (TreeKeyHandler's first guard), so a reachable-but-
                // unhosted modal would soft-lock the pane with no visible
                // UI. Reachable set: delete (d), create (a), rename (r),
                // fuzzy finder (f). Zoxide + context-menu are Miller-only
                // — intentionally not hosted.
                //
                // The fuzzy finder (f, and IDE-wide Ctrl+F) is hosted at
                // WINDOW scope (ideFuzzyFinder, after the root layout) —
                // it shares this WindowState, so the tree's `f` key still
                // opens it, but the dialog centers over the whole window
                // and works from every central surface, sidebar hidden
                // included.
                //
                // Scoped to the MAIN tree only: GitStatusPanel's embedded
                // tree stays navigation-only (no windowState), which lets
                // RenamePopup's positional bindings assume fileTreeView
                // coordinates unconditionally. Extending ops to the
                // changes pane means sharing this WindowState and making
                // the positional trio activeTreeSubPane-aware.
                FmUi.WindowState {
                    id: treeOpsWindowState
                    // Live binding chain: initialPath → currentPath (the
                    // fuzzy finder searches from currentPath), so the
                    // search root follows the anchored project root.
                    initialPath: controller.displayedRoot
                }

                // Re-pin currentPath whenever the project root changes —
                // anchor toggle (Ctrl+Shift+P / :SymmetriaAnchor),
                // terminal/nvim cd while unanchored, or after a
                // bookmark-chord navigate() broke the initialPath →
                // currentPath binding (navigate assigns currentPath
                // imperatively; QML bindings don't survive imperative
                // writes). Without this, one g-chord bookmark jump would
                // permanently detach the fuzzy finder's search root from
                // the project root.
                //
                // Modal guard: navigate() unconditionally closes any open
                // modal (correct FM semantics — changing directory cancels
                // in-flight dialogs). But here a root change can arrive
                // from OUTSIDE the sidebar (an unanchored cd in the
                // terminal) while the user is mid-rename, discarding their
                // typed input. Operations target absolute paths, so the
                // modal stays valid across a re-root — assign currentPath
                // directly in that case and let the user finish.
                Connections {
                    target: controller
                    function onDisplayedRootChanged(): void {
                        if (treeOpsWindowState.activeModal === treeOpsWindowState.modalNone)
                            treeOpsWindowState.navigate(controller.displayedRoot);
                        else
                            treeOpsWindowState.currentPath = controller.displayedRoot;
                    }
                }

                FmUi.DeleteConfirmPopup {
                    anchors.fill: parent
                    windowState: treeOpsWindowState
                }
                FmUi.CreateFilePopup {
                    anchors.fill: parent
                    windowState: treeOpsWindowState
                }
                FmUi.RenamePopup {
                    anchors.fill: parent
                    windowState: treeOpsWindowState
                    // FM contract: coordinates relative to the popup's
                    // parent (treeScope here). The tree fills
                    // mainTreeScope, which sits inside the ColumnLayout
                    // at treeScope's origin — one offset hop suffices.
                    targetItemY: mainTreeScope.y + fileTreeView.currentItemBottomY
                    targetColumnX: mainTreeScope.x + fileTreeView.currentColumnX
                    targetColumnWidth: fileTreeView.currentColumnWidth
                }
                // Chord hints (g/c/, which-key). Non-modal — opacity-
                // gated internally; renders only while a chord prefix
                // or bookmark sub-mode is active.
                FmUi.WhichKeyPopup {
                    anchors.fill: parent
                    windowState: treeOpsWindowState
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
                if (root.activeTreeSubPane === 1 && gitStatusPanel.reachable)
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
            // The editor QMLTermWidget accepts keyboard focus directly, so a
            // direct forceActiveFocus on `editor` lands correctly without the
            // descendant-walker workaround needed for the tree direction.
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
            // The terminal QMLTermWidget accepts focus directly, so a direct
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
        // `gitStatusPanel.reachable`, so the symmetric guard there
        // prevents the bad state from being re-entered while the
        // pane is hidden OR collapsed.
        //
        // Keyed on `reachable` (not `visible`) so this fires for BOTH
        // ways the panel stops accepting focus: the clean-tree hard-hide
        // (visible:false) AND the git-surface fold (collapsed:true, where
        // visible stays true). Either transition orphans focus the same
        // way, so they share one release path.
        Connections {
            target: gitStatusPanel
            function onReachableChanged(): void {
                if (gitStatusPanel.reachable || root.activeTreeSubPane !== 1)
                    return;
                root.activeTreeSubPane = 0;
                // If the side panel currently has focus, the previously-
                // active sub-pane is the one that just went away — its
                // inner items can no longer hold activeFocus. Hand focus
                // to the main tree so the user can keep navigating without
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
                // Resolved-root-relative path (= GitStatusListModel.displayName).
                // The git viewer's changes tree keys its working-diff request
                // on this; it MUST be relative to the resolved repo toplevel
                // (the `git diff` cwd), not the asked `repoRoot`. The FM tree
                // ignores this extra field.
                displayName: s.path,
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
    // Agent spawn menu — Window-level modal overlay (Ctrl+Shift+A).
    // On dismiss (Esc) focus returns to whatever surface is visible via
    // the same dispatch onActiveChanged uses; on spawn, focus_agent's
    // focusAgentRequested handles the handoff to the new terminal.
    AgentSpawnMenu {
        id: agentSpawnMenu
        onDismissed: root._restoreCentralFocus()
        // OpenCode resume needs a session id (`--session` has no
        // interactive picker form) — chain into the session picker.
        onResumePickerRequested: dangerous => agentSessionPicker.open(dangerous)
    }

    // OpenCode session picker — the resume path for the OpenCode
    // harness (spawns `opencode --session <id>` on accept).
    AgentSessionPicker {
        id: agentSessionPicker
        onDismissed: root._restoreCentralFocus()
    }

    // Per-project MCP toggles — Window-level modal overlay (Ctrl+Shift+M).
    // `w` toggles the chrome-devtools MCP for this project's agents; the
    // marker it writes is committable (travels with the repo). On dismiss
    // focus returns to the visible surface via the same dispatch the other
    // modals use.
    McpMenu {
        id: mcpMenu
        onDismissed: root._restoreCentralFocus()
    }

    // Window-close confirmation (Hyprland Super+Q / WM close). Closing the
    // IDE reaps every terminal-agent + the editor session at once, so the
    // close request is vetoed in `onClosing` below and routed here first —
    // a deliberate Enter (or click) confirms; Esc/Cancel returns to work.
    ConfirmDialog {
        id: closeConfirmDialog
        title: "Close this session?"
        message: "This window's agents and editor will be closed."
        confirmText: "Close"
        // Route through the teardown funnel's proceed primitive (NOT Qt.quit /
        // bare authorize_and_quit) so the close honours the close-vs-reload
        // intent set in request_teardown. The session is saved by shutdown()
        // on the way out regardless of this branch.
        onConfirmed: controller.teardown_discard()
        onCancelled: root._restoreCentralFocus()
    }

    // 3-way unsaved-changes gate (opened by request_teardown when buffers are
    // dirty, for both close and reload). save / discard route to the matching
    // teardown_* slot; hold aborts and returns focus to work.
    UnsavedChangesDialog {
        id: unsavedChangesDialog
        onSaveChanges: controller.teardown_save_all()
        onDiscardChanges: controller.teardown_discard()
        onHeld: root._restoreCentralFocus()
    }

    // Saved-session view (Ctrl+Shift+S) — see / restore the project's session.
    SessionsMenu {
        id: sessionsMenu
        onDismissed: root._restoreCentralFocus()
    }

    // Teardown funnel → the right confirmation. request_teardown does the one
    // dirty-buffer check, then: dirty → the 3-way dialog; clean close → the
    // confirm; clean reload proceeds without a dialog (in the slot itself).
    Connections {
        target: controller
        function onCleanTeardownRequested() {
            closeConfirmDialog.open();
        }
        function onDirtyTeardownRequested(paths) {
            unsavedChangesDialog.open(paths);
        }
    }

    // === IDE-wide fuzzy file finder (Ctrl+F, or `f` in the tree) ===
    //
    // The FM's FuzzyFinderPopup hosted at WINDOW scope — the same fff
    // engine the user's fff.nvim binds to Ctrl+F in standalone nvim,
    // surfaced as a native overlay that works from every central
    // surface (editor, terminal, agent, FM). Rides treeOpsWindowState:
    //   - search root = currentPath = controller.displayedRoot (the
    //     anchored project root; the Connections block in treeScope
    //     keeps it pinned across re-roots), and
    //   - the tree's `f` key (TreeKeyHandler → requestFuzzyFinder on
    //     this same WindowState) opens this instance too — one popup,
    //     two entry points.
    // externalActivation replaces the FM-native "navigate + focus in
    // list" confirm flow with the activated() signal handled below.
    FmUi.FuzzyFinderPopup {
        id: ideFuzzyFinder

        // Captured at open: was the sidebar tree the focus owner? The
        // Loader's `active` flips synchronously with activeModal (the
        // popup body loads async after), so at this instant the
        // pre-open owner still holds activeFocus and the capture is
        // race-free. Drives ONLY the cancel path (Esc/scrim) — on
        // activation the onActivated handler owns focus instead.
        property bool returnFocusToTree: false

        // Set by onActivated so the deferred cancel-focus settle below
        // knows an activation already claimed focus and stands down.
        property bool _activationHandled: false

        anchors.fill: parent
        windowState: treeOpsWindowState
        externalActivation: true

        // FOCUS-ORDERING HAZARD (shared WindowState). The sidebar tree
        // rides this same treeOpsWindowState, and the FM's FileTreeView
        // re-grabs focus on EVERY modal→none transition via its own
        // DEFERRED Qt.callLater (FileTreeView.qml onActiveModalChanged:
        // "Any modal closing returns focus to the view"). Because that
        // grab is deferred, any focus we set SYNCHRONOUSLY while the
        // finder closes is stomped one tick later by the tree. The fix
        // is to also defer our focus grant via Qt.callLater, scheduling
        // it from a point that out-orders the tree's:
        //   - activation: scheduled in onActivated, which fires AFTER
        //     closeModal() (where the tree's callLater is registered),
        //     so ours is registered later and runs later in the same
        //     drain — it wins, with no extra-frame flicker.
        //   - cancel: a tree-origin cancel lets the tree's own grab win
        //     (correct, no action from us); a central-origin cancel
        //     double-defers to land after that grab and reclaim the
        //     central surface (see the cancel branch below).
        // A cleaner long-term fix is a dedicated WindowState for the
        // finder (the tree handler would never fire), but that needs the
        // tree's bare-`f` key — hardwired to its own WindowState — to be
        // redirected, which the installed FM module doesn't expose.
        onActiveChanged: {
            if (active) {
                // Reset per open. _activationHandled gates the cancel
                // settle below; its reset relies on every reopen passing
                // through modalNone first (the active false→true edge).
                // The Ctrl+F toggle and Esc both closeModal() before any
                // reopen, so that edge always fires.
                returnFocusToTree = treeScope.activeFocus;
                _activationHandled = false;
                return;
            }
            // CANCEL path (Esc/scrim). The sidebar tree shares this
            // WindowState and re-grabs focus to ITSELF on modal→none via
            // its own deferred Qt.callLater (FileTreeView.qml
            // onActiveModalChanged), so a cancel that ORIGINATED from the
            // tree needs nothing from us — the tree self-restores. A
            // central-origin cancel must OVERRIDE that grab; the two
            // single callLaters are scheduled off the same activeModal
            // change in UNSPECIFIED relative order, so a single defer
            // could lose the race. DOUBLE-defer instead: the inner
            // callLater is scheduled during the first drain and therefore
            // runs in the NEXT drain, strictly after the tree's single
            // callLater. Cost: one transient frame where the sidebar
            // holds focus before the central surface reclaims it.
            if (!returnFocusToTree)
                Qt.callLater(() => Qt.callLater(_settleCancelFocus));
        }

        function _settleCancelFocus(): void {
            // active: the finder was reopened before this deferred settle
            //   drained — leave its focus alone. _activationHandled: a
            //   file/dir was picked, so onActivated owns focus.
            // (returnFocusToTree is already excluded at the scheduling
            //   site above — a tree-origin cancel never schedules this.)
            if (active || _activationHandled)
                return;
            root._restoreCentralFocus();
        }

        // Fires AFTER closeModal() — so a Qt.callLater scheduled here is
        // registered after the tree's modal-close refocus and runs after
        // it, giving our grant the final word (see the hazard note above).
        onActivated: (path, isDir) => {
            _activationHandled = true;
            if (isDir) {
                // Directories aren't editor material — hand off to the
                // FM central surface navigated there. The FM's FileList
                // claims focus itself on construction (see the no-
                // forceActiveFocus comment on fmPane), so deferring the
                // surface swap past the tree's grab is enough.
                Qt.callLater(() => controller.show_fm(path));
            } else {
                // Open immediately (surface swap + :edit), but defer the
                // focus grant so it beats the tree's deferred grab.
                controller.open_in_nvim(path);
                Qt.callLater(() => editor.forceActiveFocus());
            }
        }
    }

    /// Shared focus dispatch: pull active focus into the visible central
    /// surface. Used by Window.onActiveChanged and modal dismissals.
    function _restoreCentralFocus() {
        // Modal guard: window re-activation (Alt-Tab away and back) must
        // not yank focus out of an open spawn menu — that left the menu
        // visible but deaf, with every key falling through to the surface
        // underneath and no way to dismiss it. reassert() — NOT open() —
        // re-grabs the key catcher without resetting harness to Claude
        // (see AgentSpawnMenu.reassert). Mirrors the session-picker branch.
        if (closeConfirmDialog.visible) {
            // Same deafness hazard as the spawn menu: Alt-Tab back must
            // not yank focus out of the open close-confirm, leaving it
            // visible but unable to accept Enter/Esc.
            closeConfirmDialog.reassert();
            return;
        }
        if (unsavedChangesDialog.visible) {
            unsavedChangesDialog.reassert();
            return;
        }
        if (sessionsMenu.visible) {
            sessionsMenu.reassert();
            return;
        }
        if (agentSpawnMenu.visible) {
            agentSpawnMenu.reassert();
            return;
        }
        if (mcpMenu.visible) {
            mcpMenu.reassert();
            return;
        }
        if (agentSessionPicker.visible) {
            // reassert(), not open() — open() re-fetches the session list.
            agentSessionPicker.reassert();
            return;
        }
        if (ideFuzzyFinder.active && ideFuzzyFinder.item) {
            // Same deafness hazard as the spawn menu: Alt-Tab back must
            // not yank focus out of the open finder. The loaded item is
            // a FocusScope whose TextInput holds `focus: true`, so the
            // grant lands back on the search field.
            ideFuzzyFinder.item.forceActiveFocus();
            return;
        }
        if (controller.fmVisible && fmPaneLoader.item)
            fmPaneLoader.item.forceActiveFocus();
        else if (controller.agentVisible)
            agentPane.forceActiveFocus();
        else if (controller.agentSurfaceVisible)
            controller.focus_agent(controller.focusedAgent);
        else if (controller.terminalVisible)
            terminalView.forceActiveFocus();
        else
            editor.forceActiveFocus();
    }

    onActiveChanged: {
        if (!active)
            return;
        root._restoreCentralFocus();
    }

    // Window-close guard. Hyprland's Super+Q (`killactive`) — and any WM
    // close button — closes the Wayland toplevel, which Qt surfaces as
    // this `closing` signal. Veto it (`close.accepted = false`) and raise
    // the confirm dialog instead, so a stray kill chord can't tear down
    // the whole workspace (every terminal-agent + the editor session) in
    // one keystroke.
    //
    // Re-entrancy: on Qt-for-Wayland here, quit() ITSELF re-emits `closing`
    // as it tears the toplevel down — so a naive unconditional veto reopens
    // the dialog forever and the app never quits (observed). The fix is a
    // single quit choke point: every PROGRAMMATIC quit goes through
    // controller.authorize_and_quit(), which sets controller.quitAuthorized
    // BEFORE quitting. The genuine WM/Super+Q close reaches here DIRECTLY
    // (no prior quit() call), so quitAuthorized is still false → we prompt.
    // The re-entrant `closing` from the subsequent quit sees it true →
    // passes through (quitOnLastWindowClosed default-true then ends the
    // app → aboutToQuit → shutdown).
    //
    // This also correctly NON-prompts the nvim-quit paths (`:qa` →
    // editorSession.onFinished, and the RPC `backend.closed` wire): both
    // route through authorize_and_quit, so they tear down without a dialog
    // over an already-dead editor (which a cancel would otherwise strand).
    onClosing: function (close) {
        if (controller.quitAuthorized)
            return; // accepted stays true → let the close proceed
        close.accepted = false;
        // Make the close-confirm the ONLY modal. A spawn menu / session
        // picker left open when Super+Q fires would fight it for focus:
        // both are ModalOverlays whose keyCatcher self-heals (re-grabs
        // focus on the next tick while still visible), so two stacked ones
        // ping-pong focus every tick and the close dialog never reliably
        // receives Enter/Esc. Dismissing them first (their dismissed →
        // _restoreCentralFocus is harmless here — open() re-grabs focus
        // immediately after) leaves a single live modal. The fuzzy finder
        // has no such per-tick self-heal, so it can't ping-pong and is
        // left as-is.
        if (agentSpawnMenu.visible)
            agentSpawnMenu.dismiss();
        if (mcpMenu.visible)
            mcpMenu.dismiss();
        if (agentSessionPicker.visible)
            agentSessionPicker.dismiss();
        if (sessionsMenu.visible)
            sessionsMenu.dismiss();
        // A visible unsavedChangesDialog means a teardown is already in flight;
        // request_teardown below re-runs the dirty-check and re-opens the right
        // dialog, so dismissing the stale one keeps a single live modal.
        if (unsavedChangesDialog.visible)
            unsavedChangesDialog.dismiss();
        // Route through the teardown funnel (was closeConfirmDialog.open()): it
        // does the dirty-buffer check and opens the confirm OR the 3-way dialog
        // via the Connections above. A WM close is reload:false.
        controller.request_teardown(false);
    }

    // Responsive-sidebar focus recovery. When `treeVisible` flips to false
    // (window tiled narrow below SIDEBAR_MIN_WINDOW_WIDTH), the FocusScope
    // it gates becomes invisible — if the user was navigating the tree at
    // that moment, Qt drops active focus and keystrokes fall into a focus
    // black-hole. Hand focus back to the active central surface. Safe to
    // call unconditionally on hide: `_restoreCentralFocus()` re-asserts any
    // open modal and is a no-op when the central surface already holds
    // focus, so an ordinary resize while the editor is focused costs
    // nothing. Guarded to the hide direction only — on show we leave focus
    // wherever the user left it.
    //
    // Startup interaction: if the IDE launches below the threshold (a
    // Hyprland rule tiling it into a narrow column), the width seed in the
    // startup Component.onCompleted flips `treeVisible` false and fires this
    // handler during construction. That is harmless — `_restoreCentralFocus`
    // routes to the same central surface the startup focus dispatch targets,
    // so the two agree regardless of order. (The seed runs AFTER that
    // dispatch anyway; see the seed comment below.)
    Connections {
        target: controller
        function onTreeVisibleChanged() {
            if (!controller.treeVisible)
                root._restoreCentralFocus();
        }
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

        // Seed the controller with the real startup width so the responsive
        // sidebar gate (treeVisible) is correct on first paint — the
        // `onWidthChanged` comment on the Window root explains why the live
        // handler misses the construction-time value. Placed AFTER the focus
        // dispatch so a future grep-window test keyed on the dispatch block
        // isn't pushed off the end by this comment (and, per the Connections
        // handler above, so any sub-threshold-startup focus recovery runs
        // after the dispatch).
        controller.set_window_width(width);
    }
}
