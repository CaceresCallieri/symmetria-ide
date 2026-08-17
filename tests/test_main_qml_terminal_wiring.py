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

import re
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
    assert "!controller.agentVisible && !controller.fmVisible" in main_qml
    # Since the VPS toggle the local pane ALSO gates on the location (the
    # remote pane owns the terminal surface in the vps location).
    local_pane_idx = main_qml.index("id: terminalView")
    assert (
        'controller.location === "local"'
        in main_qml[local_pane_idx : local_pane_idx + 800]
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
    raise AssertionError(f"unbalanced braces from declaration at index {decl_start}")


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
    assert "_focusTerminalPane()" in dispatch_block
    assert "controller.focus_agent(controller.focusedAgent)" in dispatch_block
    # Modal guard: re-activation must NOT yank focus out of an open spawn
    # menu (visible-but-deaf menu regression). The guard must run before
    # any surface branch — check against the earliest branch (fmPaneLoader)
    # so a future reordering that moves the guard between branches is caught.
    guard_idx = dispatch_block.find("agentSpawnMenu.visible")
    fm_idx = dispatch_block.find("fmPaneLoader")
    terminal_idx = dispatch_block.find("_focusTerminalPane()")
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
    # Terminal focus routes through the location-aware dispatch since the
    # VPS toggle (two panes, one visible per location).
    assert "_focusTerminalPane()" in body


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
# Side-panel tabs — files ↔ changes, one visible at a time, toggled by Tab.
# The two panes were co-mounted and stacked vertically before this; every
# assertion here guards a failure mode that is silent rather than loud.
# ---------------------------------------------------------------------------


def test_side_panel_tab_bodies_are_mutually_exclusive(main_qml: str):
    """Both tab bodies must gate `visible` on `sidePanelTab`.

    If either loses its gate the two trees render stacked again — which
    looks like a layout bug rather than a missing binding, and the panel
    silently returns to the split column the tabs replaced.
    """
    assert "visible: root.sidePanelTab === 1" in main_qml, (
        "GitStatusPanel must be visible only on the changes tab."
    )
    assert "visible: root.sidePanelTab === 0" in main_qml, (
        "mainTreeScope must be visible only on the files tab."
    )


def test_side_panel_tab_bodies_are_not_loaded(main_qml: str):
    """The bodies swap on `visible`, never through a Loader.

    A Loader would remount the FM tree on every toggle, discarding its
    expanded-path state and scroll position and re-running the mount race
    documented at `_applyTreeMount()`. That regression is invisible in a
    screenshot and only shows up as "the tree keeps collapsing".
    """
    tabs_idx = main_qml.find("id: sidePanelTabs")
    tree_idx = main_qml.find("id: fileTreeView")
    assert tabs_idx >= 0 and tree_idx > tabs_idx
    assert "Loader" not in main_qml[tabs_idx:tree_idx]


def test_side_panel_tab_toggle_is_focus_chain_not_shortcut(main_qml: str):
    """Tab must be handled by a `Keys.onPressed` on the side panel, NOT by
    a `Shortcut`.

    An application-scope Tab would fire from every surface and cost the
    terminal its completion key and nvim its own Tab. Scoped to the focus
    chain it is inert unless the side panel already holds focus. This
    asserts the mechanism, not just the behaviour, because a well-meaning
    refactor to "make the chord consistent with its siblings" would
    reintroduce exactly that regression.
    """
    assert 'sequences: ["Tab"]' not in main_qml
    assert "Qt.Key_Tab || event.key === Qt.Key_Backtab" in main_qml
    assert "root._toggleSidePanelTab()" in main_qml


def test_side_panel_tab_click_and_key_share_a_focus_path(main_qml: str):
    """Clicking a segment and pressing Tab must both route focus through
    `_focusSidePanelTab()`.

    If the click path skips it, focus stays wherever it was and the user's
    next Tab press reads as a dead key — the click "worked" visually, so
    the bug presents as an unrelated broken keybind.
    """
    assert main_qml.count("root._focusSidePanelTab()") >= 2


def test_stacked_side_panel_properties_are_gone(main_qml: str):
    """`maxHeight` and `collapsed` on GitStatusPanel must not be re-bound.

    Both existed only because two trees shared one column: the cap kept a
    huge changeset from pushing the file tree off-screen, and the fold got
    the mini-tree out of the way of the central git surface. Re-adding
    either alongside the tabs would clamp or hide a tab body the user
    explicitly selected.
    """
    panel_idx = main_qml.find("GitStatusPanel {")
    tree_idx = main_qml.find("id: mainTreeScope")
    assert panel_idx >= 0 and tree_idx > panel_idx
    # Strip `//` comment lines first — the block deliberately NAMES both
    # properties in prose, to record why they must not come back. Matching
    # the documentation of a removal as if it were the removal undone is
    # the classic way this style of structural test turns into noise.
    panel_block = "\n".join(
        line
        for line in main_qml[panel_idx:tree_idx].splitlines()
        if not line.lstrip().startswith("//")
    )
    assert "maxHeight:" not in panel_block
    assert "collapsed:" not in panel_block


def _shortcut_body(main_qml: str, sequence: str) -> str:
    """Slice one Shortcut's own body, from its `sequences:` line to the
    closing brace at the block's indentation.

    A fixed character window does NOT work here: Main.qml's Shortcuts sit
    directly beside each other with long comment blocks between them, so a
    generous window bleeds into the NEXT chord's prose and matches names it
    legitimately discusses (the Ctrl+S block right below this one explains
    `toggle_tree`, which is exactly what the Ctrl+Shift+D test asserts is
    absent).
    """
    start = main_qml.find(f'sequences: ["{sequence}"]')
    assert start >= 0, f"{sequence} must be bound."
    end = main_qml.find("\n    }\n", start)
    assert end > start, f"{sequence}'s Shortcut block is not closed as expected."
    return main_qml[start:end]


def test_side_panel_tab_chord_exists_at_application_scope(main_qml: str):
    """Ctrl+Shift+D must switch the tab from ANY surface.

    The panel-scoped Tab key only works once you are already in the panel;
    this chord is the "look at the other tab without leaving the editor"
    half. Application scope is what makes it fire from nvim insert mode and
    from a focused terminal.
    """
    block = _shortcut_body(main_qml, "Ctrl+Shift+D")
    assert "context: Qt.ApplicationShortcut" in block
    assert "root._flipSidePanelTab()" in block


def test_side_panel_tab_chord_does_not_steal_focus(main_qml: str):
    """The chord must NOT call `_focusSidePanelTab()`.

    That is the entire distinction from the Tab key: this one changes what
    the panel SHOWS, Tab changes what you are navigating. Pulling focus into
    the tree here would make "glance at the changes" cost a trip back to
    whatever you were typing in — and the regression is easy to introduce by
    "unifying" the two paths on `_toggleSidePanelTab()`, which is why the
    flip is a separate function from the flip-and-focus.
    """
    block = _shortcut_body(main_qml, "Ctrl+Shift+D")
    assert "_focusSidePanelTab" not in block
    assert "_toggleSidePanelTab" not in block


def test_side_panel_tab_chord_reveals_rather_than_toggles_the_sidebar(main_qml: str):
    """The chord must call `show_tree()`, never `toggle_tree()`.

    `toggle_tree` FLIPS: on an already-visible sidebar it would hide the very
    panel the chord is about to switch, so every second press would blank the
    side panel. On a narrow window it would additionally set user intent to
    False, keeping the sidebar hidden after the window widens again.
    """
    block = _shortcut_body(main_qml, "Ctrl+Shift+D")
    assert "controller.show_tree()" in block
    assert "toggle_tree" not in block


@pytest.fixture(scope="module")
def git_status_panel_qml() -> str:
    """Load GitStatusPanel.qml once per module — the changes tab's body."""
    repo_root = Path(__file__).resolve().parent.parent
    return (repo_root / "qml" / "GitStatusPanel.qml").read_text()


def test_changes_panel_header_does_not_fill_height(git_status_panel_qml: str):
    """The bucket header must carry an explicit `Layout.fillHeight: false`.

    A Layout nested directly inside another Layout defaults fillHeight to
    TRUE in Qt Quick Layouts (a plain Item defaults false). Without the
    explicit override the header competes with the tab body for the
    leftover column height: the empty state drifts to the vertical middle
    of the column and the changes tree loses rows. This was latent while
    the panel was sized to its own content and only appeared once it
    became a full-height tab — so a future reader has every reason to
    delete the line as redundant. It is not.
    """
    assert "Layout.fillHeight: false" in git_status_panel_qml


def test_changes_panel_has_an_empty_state(git_status_panel_qml: str):
    """A clean working tree must draw an empty state, not hide the panel.

    The panel self-hid on `model.count > 0` while it was stacked above the
    file tree. As a tab body that rule leaves the tab header sitting above
    a void, which reads as a broken pane rather than as a clean repo.
    """
    assert "readonly property bool hasChanges" in git_status_panel_qml
    assert "visible: !root.hasChanges" in git_status_panel_qml
    assert "visible: model && model.count > 0" not in git_status_panel_qml


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
    # dense number) instead of silently no-oping. Scope this to the actual
    # Ctrl+1..5 dispatch: Ctrl+Shift+A has another open() call and must not be
    # allowed to satisfy this branch's contract.
    instantiator_idx = main_qml.index("model: controller.maxAgentSlots")
    instantiator_decl = main_qml.rfind("Instantiator", 0, instantiator_idx)
    dispatch = _extract_braced_body(main_qml, instantiator_decl)
    alias = re.search(r"\bvar\s+(\w+)\s*=\s*agentSpawnMenu\s*;", dispatch)
    receivers = ["agentSpawnMenu", *(alias.groups() if alias is not None else ())]
    executable_dispatch = re.sub(r"//[^\n]*", "", dispatch)
    assert any(
        re.search(
            rf"else\s+if\s*\(\s*{receiver}\.visible\s*\)\s*\{{?\s*"
            rf"{receiver}\.reassert\(\)\s*;[\s\S]*?"
            rf"else\s*\{{\s*{receiver}\.open\(\)\s*;",
            executable_dispatch,
        )
        is not None
        for receiver in receivers
    ), "the Ctrl+1..5 empty-slot branch must open only a closed chooser"


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
    # Scrollback routes through the fork's dedicated `scrollPageFraction` slot
    # (NOT `simulateWheel`, which would re-encode as VT keys and reach claude's
    # own pager) — see the scrollFocusedAgent comment in Main.qml.
    assert "scrollPageFraction(" in main_qml
    assert (
        "term.scrollPageFraction(controller.focusedAgentScrollFraction(), direction)"
        in main_qml
    )
    assert "term.scrollPageFraction(0.167, direction)" not in main_qml


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


# ---------------------------------------------------------------------------
# VPS location: the remote terminal pane (Phase 5 of the location toggle)
# ---------------------------------------------------------------------------


def test_remote_terminal_pane_exists_behind_a_loader(main_qml: str):
    """The vps terminal is a Loader-wrapped second QMLTermWidget — lazy (no
    ssh child until the first vps entry) and independent of the local pane
    (which must stay alive across toggles)."""
    assert "id: remoteTerminalLoader" in main_qml
    assert "controller.remote_shell_argv()" in main_qml


def test_terminal_panes_are_location_exclusive(main_qml: str):
    """Exactly one terminal pane per location: the local pane gates on
    location === "local", the remote pane on === "vps" — both under the
    same terminalVisible surface flag."""
    assert 'controller.location === "local"' in main_qml
    local_gate = main_qml.index('controller.location === "local"')
    # The local pane's visible binding carries the location gate.
    assert "id: terminalView" in main_qml[: local_gate + 200]
    remote_start = main_qml.index("id: remoteTerminalLoader")
    remote_block = main_qml[remote_start : remote_start + 600]
    assert 'controller.location === "vps"' in remote_block


def test_remote_pane_has_no_cwd_poll_timer(main_qml: str):
    """The cwd-poll Timer belongs ONLY to the local pane: the remote pane's
    currentDir is the LOCAL ssh process's cwd — feeding it to on_shell_cwd
    would corrupt the anchor state machine."""
    remote_start = main_qml.index("id: remoteTerminalLoader")
    # The remote pane is the last terminal block before the agent surface.
    remote_block = main_qml[remote_start : main_qml.index("id: agentSurface")]
    assert "on_shell_cwd" not in remote_block
    assert "Timer" not in remote_block


# ---------------------------------------------------------------------------
# QML → Python seam: every controller.<name>(…) must exist on AppController
# ---------------------------------------------------------------------------


def test_every_controller_call_in_main_qml_is_callable(main_qml: str):
    """A `controller.foo()` QML typo is a RUNTIME TypeError, not a load error.

    QML resolves the call when the binding runs, so a one-sided rename (or a
    plain typo) stays silent until the user presses the key that reaches it —
    exactly how `controller.focusedAgentScrollFraction()` would fail: Ctrl+U/D
    would simply stop scrolling, with nothing on stdout unless the QML message
    handler happened to be watched.

    Names are read off the source rather than enumerated here, so a new call
    site is covered the moment it lands. Dynamic constructs (a computed method
    name) would not be caught — there are none today, and this asserts the
    regex found a plausible number of calls so a future refactor that hides
    every call behind one cannot pass vacuously.
    """
    from symmetria_ide.app import AppController

    names = sorted(set(re.findall(r"\bcontroller\.([A-Za-z_]\w*)\s*\(", main_qml)))
    assert len(names) > 20, "regex stopped matching Main.qml's call sites"
    assert "focusedAgentScrollFraction" in names
    missing = [n for n in names if not callable(getattr(AppController, n, None))]
    assert missing == [], f"Main.qml calls non-existent AppController slots: {missing}"


def test_terminal_focus_routes_through_location_dispatch(main_qml: str):
    """Every focus-the-terminal site must use _focusTerminalPane (the
    location-aware dispatch) — a direct terminalView.forceActiveFocus()
    outside the helper would focus the HIDDEN local pane in vps."""
    assert "function _focusTerminalPane()" in main_qml
    helper_start = main_qml.index("function _focusTerminalPane()")
    helper_block = main_qml[helper_start : helper_start + 400]
    assert "remoteTerminalLoader.item.forceActiveFocus()" in helper_block
    # Outside the helper body, no direct terminalView focus calls remain
    # (the comment at the Ctrl+H NOTE doesn't count — code only).
    outside = main_qml[:helper_start] + main_qml[helper_start + 400 :]
    direct_calls = [
        line
        for line in outside.splitlines()
        if "terminalView.forceActiveFocus()" in line
        and not line.strip().startswith("//")
    ]
    assert direct_calls == []
