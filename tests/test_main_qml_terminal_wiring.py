"""Structural smoke tests for Main.qml's PR 5 terminal wiring.

QML files don't have a hermetic unit-test surface in this codebase
(no pytest-qt, instantiating Main.qml needs a full QGuiApplication
+ window). We use the same pattern as `test_default_font.py` /
`test_transparent_mode.py` — read the file, regex-assert the load-
bearing structural pieces are present.

These tests catch the kind of regressions that a "refactor Main.qml"
PR could silently introduce: a missing Shortcut block (chord stops
working), a focus-handoff branch that doesn't consider terminalVisible
(alt-tab back leaves user typing into a dead surface), a TerminalView
binding missing its backend connection (terminal renders blank).
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def main_qml() -> str:
    """Load Main.qml once per module."""
    repo_root = Path(__file__).resolve().parent.parent
    return (repo_root / "qml" / "Main.qml").read_text()


# ---------------------------------------------------------------------------
# Application-scope chord — Ctrl+Shift+E toggles editor↔terminal
# ---------------------------------------------------------------------------


def test_editor_terminal_toggle_chord_exists(main_qml: str):
    """Ctrl+Shift+E must toggle editor↔terminal via the
    `toggle_editor_terminal` slot, bound at QApplicationShortcut scope
    so it wins over NvimView's keyboard capture even in insert mode.

    Both E and T are now swap-last-two toggles (forward to their named
    surface, back to where you came from); they differ only in which
    surface they name (see test_direct_terminal_chord_present for T).
    """
    assert "Ctrl+Shift+E" in main_qml
    assert "controller.toggle_editor_terminal()" in main_qml


def test_direct_terminal_chord_present(main_qml: str):
    """Ctrl+Shift+T must call `toggle_terminal_home` — a toggle, not a
    one-way jump. Forward (from any other surface) → terminal; pressed
    while already on the terminal → BACK to the previous surface.

    The terminal is the home base but no longer a privileged forced
    fallback: every surface chord (E/T/G/B) now swaps between your two
    most-recent surfaces (single previous-pointer; vim Ctrl-^ semantics).
    `swap_to_terminal` survives as the one-way "go home, no bounce"
    primitive for file-open/tests, so scope the wiring assertion to the
    chord BLOCK — the bare `toggle_terminal_home` substring is unique, but
    block-scoping keeps the assertion robust to future edits.
    """
    chord_idx = main_qml.find('sequences: ["Ctrl+Shift+T"]')
    assert chord_idx >= 0, "Ctrl+Shift+T Shortcut block not found in Main.qml"
    # Brace-match the enclosing Shortcut block via the shared helper rather
    # than a fixed-size window — a future `enabled:` guard or extra comment
    # inside the block could push the assertions past a hard char window and
    # silently false-negative (same robustness rationale as
    # test_window_activation_considers_terminal_visible). `sequences:` sits
    # inside the block, so rewind to the `Shortcut` decl first.
    shortcut_decl = main_qml.rfind("Shortcut", 0, chord_idx)
    assert shortcut_decl >= 0
    chord_block = _extract_braced_body(main_qml, shortcut_decl)
    assert "controller.toggle_terminal_home()" in chord_block
    assert "Qt.ApplicationShortcut" in chord_block


def test_swap_chord_uses_application_shortcut_context(main_qml: str):
    """The editor↔terminal toggle chord must use Qt.ApplicationShortcut,
    not the default Qt.WindowShortcut — without that, NvimView's key
    handler captures the chord before it can fire. We assert a floor on
    total ApplicationShortcut blocks (anchor, surface toggles, the agent
    chord family, paste, etc.) to catch a regression that silently demotes
    chords wholesale. The floor (20) sits below the real count (~25) with
    headroom for a few legitimate chord removals; per-chord context is
    verified by the individual chord tests, so this is only a bulk-demotion
    backstop, not a per-chord guard."""
    assert main_qml.count("Qt.ApplicationShortcut") >= 20


# ---------------------------------------------------------------------------
# Editor pane is a QMLTermWidget hosting nvim over the --listen socket
# ---------------------------------------------------------------------------


def test_qmltermwidget_imported(main_qml: str):
    """Main.qml must import the forked QMLTermWidget module — without it the
    editor + shell panes fail to resolve at engine load."""
    assert "import QMLTermWidget 2.0" in main_qml


def test_editor_pane_runs_nvim_over_listen_socket(main_qml: str):
    """The editor pane's QMLTermSession must launch nvim via the editorProgram/
    editorArgs context props (which carry `--listen <socket>` + the runtime
    injection). The RPC chrome relay rides that socket; if the launch wiring
    breaks, NvimBackend attaches to a socket nothing ever binds."""
    assert "id: editor" in main_qml
    assert "shellProgram: editorProgram" in main_qml
    assert "shellProgramArgs: editorArgs" in main_qml
    assert "editorSession.startShellProgram()" in main_qml


def test_editor_pane_transparency_invariants(main_qml: str):
    """Background transparency requires useFBORendering:false + transparent
    fillColor + the Symmetria scheme on BOTH panes. Dropping any of these
    silently returns to an opaque terminal (CLAUDE.md "The terminal panes")."""
    assert main_qml.count("useFBORendering: false") >= 2
    assert main_qml.count('colorScheme: "Symmetria"') >= 2
    assert main_qml.count('fillColor: "transparent"') >= 2


# ---------------------------------------------------------------------------
# Shell pane is a QMLTermWidget under mainContent
# ---------------------------------------------------------------------------


def test_terminal_pane_mounted(main_qml: str):
    """The shell pane must be instantiated as a QMLTermWidget (the forked
    Konsole VT engine) under `mainContent`, keeping `id: terminalView` — the
    chord toggles state, but without an actual pane there's nothing to show.
    Post-qmltermwidget-migration the shell is no longer a TerminalView."""
    assert "QMLTermWidget {" in main_qml
    assert "id: terminalView" in main_qml


def test_terminal_pane_runs_shell_and_syncs_cwd(main_qml: str):
    """The shell pane's QMLTermSession must launch the shell (`shellExec`
    context prop) and its cwd must feed `controller.on_shell_cwd` — the
    replacement for the old OSC 7 → terminalBackend.osc7_received path. The
    `currentDir` poll keeps the file tree following `cd` in the shell."""
    assert "shellProgram: shellExec" in main_qml
    assert "controller.on_shell_cwd(" in main_qml
    assert "shellSession.currentDir" in main_qml


def test_terminal_view_visibility_gated(main_qml: str):
    """TerminalView visibility must depend on terminalVisible — not
    raw centralSurface comparison or hardcoded true/false. Also must
    factor in agentVisible so the agent overlay hides BOTH central
    surfaces."""
    assert "controller.terminalVisible" in main_qml
    assert (
        "!controller.agentVisible && !controller.fmVisible && controller.terminalVisible"
        in main_qml
    )


def test_editor_visibility_tightened_for_terminal(main_qml: str):
    """NvimView's visibility must now require BOTH editorVisible AND
    not-agentVisible. Pre-PR-5 it was just `!agentVisible`, which
    would leave the editor visible underneath the terminal pane."""
    assert (
        "!controller.agentVisible && !controller.fmVisible && controller.editorVisible"
        in main_qml
    )


# ---------------------------------------------------------------------------
# Focus handoffs — onFocusTerminalRequested + Window-level dispatch
# ---------------------------------------------------------------------------


def test_focus_terminal_requested_handler(main_qml: str):
    """Connections block must subscribe to controller.focusTerminalRequested
    and route to terminalView.forceActiveFocus() — symmetric with the
    existing onFocusEditorRequested / onFocusTreeRequested handlers."""
    assert "function onFocusTerminalRequested()" in main_qml
    assert "terminalView.forceActiveFocus()" in main_qml


def _extract_braced_body(text: str, decl_start: int) -> str:
    """Return the `{...}` body of the declaration at ``decl_start``, brace-matched.

    Resilient to braces inside ``//`` line comments and string literals (', ",
    `), which QML/JS bodies contain — a naive depth counter would miscount on
    those. Replaces brittle fixed-offset windows into QML source that break on
    unrelated edits. Raises AssertionError if the braces never balance.
    """
    open_idx = text.index("{", decl_start)
    depth = 0
    j = open_idx
    in_str: str | None = None
    while j < len(text):
        c = text[j]
        if in_str is not None:
            if c == "\\":
                j += 2
                continue
            if c == in_str:
                in_str = None
        elif c in "'\"`":
            in_str = c
        elif c == "/" and j + 1 < len(text) and text[j + 1] == "/":
            nl = text.find("\n", j)
            j = len(text) if nl == -1 else nl
            continue
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx : j + 1]
        j += 1
    raise AssertionError("unbalanced braces from declaration at index %d" % decl_start)


def test_window_activation_considers_terminal_visible(main_qml: str):
    """Window.onActiveChanged must route to terminalView when the
    terminal is the current central surface. Without this, alt-tabbing
    back when terminal-visible leaves focus on a dead surface.

    The dispatch now lives in the shared `_restoreCentralFocus()` helper
    (also used by modal dismissals like AgentSpawnMenu's Esc) —
    onActiveChanged delegates to it.
    """
    # rfind, not find: a child element (the agent spawn menu, ~line 2132)
    # also declares `onActiveChanged:`. The Window-level handler under test
    # is the LAST occurrence — same disambiguation the sibling
    # test_startup_focus_routes_to_terminal_when_visible uses for
    # Component.onCompleted.
    on_active_idx = main_qml.rfind("onActiveChanged:")
    assert on_active_idx >= 0
    on_active_block = main_qml[on_active_idx : on_active_idx + 200]
    assert "_restoreCentralFocus()" in on_active_block
    # The shared dispatch must carry the terminal branch (and the agent
    # surface branch, which routes through focus_agent).
    dispatch_idx = main_qml.find("function _restoreCentralFocus()")
    assert dispatch_idx >= 0
    # Scope the assertions to the function's actual `{...}` body via brace
    # matching rather than a fixed character window — the body grows whenever a
    # modal guard is prepended (it has been bumped twice), and a too-small
    # window silently passes/fails on unrelated edits.
    dispatch_block = _extract_braced_body(main_qml, dispatch_idx)
    assert "terminalView.forceActiveFocus()" in dispatch_block
    assert "controller.focus_agent(controller.focusedAgent)" in dispatch_block
    # Modal guard: re-activation must NOT yank focus out of an open spawn
    # menu (visible-but-deaf menu regression). The guard must run before
    # any surface branch — check against the earliest branch (fmPaneLoader)
    # so a future reordering that moves the guard between branches is caught.
    guard_idx = dispatch_block.find("agentSpawnMenu.visible")
    fm_idx = dispatch_block.find("fmPaneLoader")
    terminal_idx = dispatch_block.find("terminalView.forceActiveFocus()")
    assert 0 <= guard_idx < fm_idx
    assert guard_idx < terminal_idx


def test_startup_focus_routes_to_terminal_when_visible(main_qml: str):
    """Window.Component.onCompleted must check terminalVisible before
    defaulting to editor focus — Q2-d topology means terminal is the
    default first-launch surface."""
    # Multiple Component.onCompleted blocks exist; we want the LAST one
    # (the Window-level startup override). 1200-char window covers the
    # comment block plus the actual dispatch logic — narrower windows
    # land inside the multi-paragraph comment before reaching the code.
    last_completed = main_qml.rfind("Component.onCompleted:")
    assert last_completed >= 0
    body = main_qml[last_completed : last_completed + 1200]
    assert "controller.terminalVisible" in body
    assert "terminalView.forceActiveFocus()" in body


def test_responsive_sidebar_focus_recovery_is_hide_only(main_qml: str):
    """The responsive-sidebar `Connections` handler must restore central
    focus ONLY when the sidebar hides. If a future edit drops the
    `if (!controller.treeVisible)` guard, focus recovery would also fire on
    SHOW — stealing focus from the editor every time the window widens past
    the threshold. Pin the hide-only guard and its ordering before the
    `_restoreCentralFocus()` call."""
    handler_idx = main_qml.find("function onTreeVisibleChanged()")
    assert handler_idx >= 0, "responsive-sidebar focus-recovery handler missing"
    # The handler body is a few lines; a 200-char window covers it.
    handler_block = main_qml[handler_idx : handler_idx + 200]
    guard_idx = handler_block.find("if (!controller.treeVisible)")
    restore_idx = handler_block.find("_restoreCentralFocus()")
    assert guard_idx >= 0, "hide-only guard missing — recovery would fire on show too"
    assert restore_idx >= 0
    assert guard_idx < restore_idx, "guard must precede the focus-recovery call"


def test_fm_overlay_restore_considers_terminal(main_qml: str):
    """When the FM overlay closes, restoration must consider terminalVisible
    alongside agentVisible — otherwise FM dismiss while terminal-visible
    leaves focus on the editor instead of the visible terminal."""
    # The FM overlay's Connections block has an onFmVisibleChanged
    # handler that branches on the various central surfaces.
    fm_idx = main_qml.find("if (!controller.fmVisible)")
    assert fm_idx >= 0
    # Body should reference terminalVisible.
    body = main_qml[fm_idx : fm_idx + 2000]
    assert "controller.terminalVisible" in body


# ---------------------------------------------------------------------------
# Coexistence with the existing pane vocabulary
# ---------------------------------------------------------------------------


def test_three_central_surfaces_distinct(main_qml: str):
    """The three sibling components under `mainContent` are NvimView,
    TerminalView, AgentPane — assert all three are present with the
    expected ids."""
    assert "id: editor" in main_qml
    assert "id: terminalView" in main_qml
    assert "id: agentPane" in main_qml


# ---------------------------------------------------------------------------
# Side-panel "where am I" header — surfaces the displayedRoot + anchor
# state above the git pane + file tree. Operationalizes the dual-mode
# (navigation vs project) framing in docs/vision.md.
# ---------------------------------------------------------------------------


def test_location_header_present_in_side_panel(main_qml: str):
    """A `locationHeader` Rectangle must sit at the top of the side
    panel's ColumnLayout, ABOVE GitStatusPanel. The header is the
    user's primary visual answer to "which project am I in right
    now" — losing it silently in a Main.qml refactor would force the
    user back to inferring project context from the status bar."""
    assert "id: locationHeader" in main_qml
    # Header is positioned above GitStatusPanel — the substring order
    # in the file is a proxy for ColumnLayout child order.
    header_idx = main_qml.find("id: locationHeader")
    git_panel_idx = main_qml.find("GitStatusPanel {")
    assert header_idx >= 0 and git_panel_idx >= 0
    assert header_idx < git_panel_idx, (
        "locationHeader must appear before GitStatusPanel in the side "
        "panel ColumnLayout — order in the file = layout order."
    )


def test_location_header_binds_displayed_root_compact(main_qml: str):
    """The header must render `controller.displayedRootCompact` (the
    HOME-collapsed view-layer transform), NOT the raw `displayedRoot`
    — otherwise paths under $HOME render with the full /home/jc/ prefix
    and chew up the header's horizontal budget."""
    assert "controller.displayedRootCompact" in main_qml


def test_location_header_reflects_anchor_state(main_qml: str):
    """The header must read `controller.anchored` so the visual
    treatment (accent color + anchor dot) flips between the dual
    modes. Without this binding the header would look identical
    whether the user has anchored or is drifting, defeating the
    "modes of inhabiting the IDE" UI thesis."""
    # Grep the immediate vicinity of the header for `controller.anchored`
    # so we don't get a false positive from the Ctrl+Shift+P anchor handler
    # elsewhere in the file.
    header_idx = main_qml.find("id: locationHeader")
    assert header_idx >= 0
    # Header block extends until the next sibling — pick a generous
    # window that covers the Rectangle + its RowLayout children.
    header_block = main_qml[header_idx : header_idx + 2000]
    assert "controller.anchored" in header_block


# ---------------------------------------------------------------------------
# Terminal-agent chord family — Ctrl+1..5 / Ctrl+Shift+A/Q/H/L / Ctrl+U/D
# (HARD CUTOVER: these chords previously relayed to orchestrator.nvim via
# controller.send_editor_keys; the IDE-native agent surface owns them now.)
# ---------------------------------------------------------------------------


def test_orchestrator_relay_absent(main_qml: str):
    """The nvim chord relay must be GONE — Ctrl+1..5 / Ctrl+Shift+Q now
    drive the IDE's own terminal-agent pool, not orchestrator.nvim
    keymaps inside the embedded editor. A reappearing
    `controller.send_editor_keys` call means the hard cutover regressed
    (likely a bad merge resurrecting commit 45a8faa's relay block).
    """
    assert "controller.send_editor_keys(" not in main_qml
    assert '"<C-S-q>"' not in main_qml


def test_agent_focus_chords_present(main_qml: str):
    """An Instantiator over maxAgentSlots must bind Ctrl+N → focus_agent.

    Always-enabled by design: focus_agent auto-switches the central
    surface, so the chords double as surface switchers; Python no-ops on
    empty slots.
    """
    assert "model: controller.maxAgentSlots" in main_qml
    # Ctrl+N addresses the Nth chip in DISPLAY order (dense, compacting),
    # so the dispatch reads controller.agentOrder, not internal slots.
    assert "controller.focus_agent(order[index])" in main_qml
    # A position with no agent opens the spawn menu (appends as next
    # dense number) instead of silently no-opping.
    assert "agentSpawnMenu.open()" in main_qml


def test_agent_management_chords_present(main_qml: str):
    """Agent menu (Ctrl+Shift+A), close (Ctrl+Shift+Q), and cycle
    (Ctrl+Shift+H/L) chords must exist; close/cycle gated on the agent
    surface so they can't fire into nothing.
    """
    assert 'sequences: ["Ctrl+Shift+A"]' in main_qml
    assert "agentSpawnMenu.open()" in main_qml
    assert 'sequences: ["Ctrl+Shift+Q"]' in main_qml
    assert "controller.close_focused_agent()" in main_qml
    assert "controller.cycle_agent_focus(-1)" in main_qml
    assert "controller.cycle_agent_focus(1)" in main_qml


def test_agent_scrollback_chords_present(main_qml: str):
    """Ctrl+U/D half-page scrollback must route through the agentSurface
    helper and stay gated off while the sidebar holds focus (the tree
    owns Ctrl+U/D for its own paging).
    """
    assert "agentSurface.scrollFocusedAgent(1)" in main_qml
    assert "agentSurface.scrollFocusedAgent(-1)" in main_qml
    assert (
        "enabled: controller.agentSurfaceVisible && !treeScope.activeFocus" in main_qml
    )
    assert "simulateWheel(" in main_qml


def test_agent_surface_pool_structure(main_qml: str):
    """The agent surface must be a FIXED Repeater over maxAgentSlots with
    per-slot Loaders — list-model churn would destroy live claude
    processes (see the agentSurface comment block).
    """
    assert "agentSlotRepeater" in main_qml
    assert "active: controller.agentSlotActive[slotLoader.index]" in main_qml
    assert "visible: controller.focusedAgent === slotLoader.slot" in main_qml
    assert "controller.agent_spawn_argv(slotLoader.slot)" in main_qml
    assert "controller.on_agent_finished(slotLoader.slot)" in main_qml
    assert "controller.on_agent_title(slotLoader.slot" in main_qml


def test_legacy_sdk_pane_env_gated(main_qml: str):
    """AgentPane (the parked Node-SDK chat) must be gated behind
    legacySdkPaneEnabled so it never co-shows with the terminal-agent
    surface in a default launch.
    """
    assert "legacySdkPaneEnabled && controller.agentVisible" in main_qml


# ---------------------------------------------------------------------------
# Clipboard paste chord — Ctrl+Shift+V → pasteClipboard() on the visible pane
# ---------------------------------------------------------------------------


def test_paste_chord_present(main_qml: str):
    """A Ctrl+Shift+V Shortcut must call pasteClipboard() on the visible
    terminal pane.

    The fork's legacy VT key encoding collapses Ctrl+Shift+V to Ctrl+V
    (the 0x1f mask strips Shift), so the widget never receives a
    distinguishable paste chord. In stock Konsole this shortcut lives in
    the embedding application — which here is Main.qml. Without this
    block, paste is silently impossible in both the editor and shell
    panes, and the STT system's sendshortcut fallback
    (`hyprctl dispatch sendshortcut CTRL SHIFT, V`) dies with it.
    """
    paste_idx = main_qml.find('sequences: ["Ctrl+Shift+V"]')
    assert paste_idx >= 0, "Ctrl+Shift+V Shortcut block not found in Main.qml"
    paste_block = main_qml[paste_idx : paste_idx + 400]
    assert "pasteClipboard()" in paste_block, (
        "pasteClipboard() not called within the Ctrl+Shift+V Shortcut block"
    )


def test_paste_chord_gated_to_terminal_surfaces(main_qml: str):
    """The paste chord must be disabled while the agent/FM surface is up —
    Qt text inputs handle Ctrl+V natively, and pasting into an unseen
    terminal would be invisible state corruption (same rationale as the
    orchestrator relay gate)."""
    paste_idx = main_qml.find('sequences: ["Ctrl+Shift+V"]')
    assert paste_idx >= 0
    paste_block = main_qml[paste_idx : paste_idx + 400]
    assert "controller.editorVisible || controller.terminalVisible" in paste_block
