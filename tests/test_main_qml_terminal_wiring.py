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

    Earlier iteration had a separate Ctrl+Shift+T chord targeting
    swap_to_terminal; that was retired in favor of the single toggle.
    """
    assert "Ctrl+Shift+E" in main_qml
    assert "controller.toggle_editor_terminal()" in main_qml


def test_old_terminal_summon_chord_retired(main_qml: str):
    """Ctrl+Shift+T was retired when the editor↔terminal toggle landed.
    If a future PR resurrects it as a chord, that's likely a regression
    (the toggle is meant to be the single user-facing entry point);
    `swap_to_terminal` remains as a Python primitive callable from
    other slots, so the assertion targets the chord BINDING (i.e. a
    `sequences: ["Ctrl+Shift+T"]` line, which is what would re-mount
    the chord) — not the bare substring, which legitimately appears
    in retirement-history comments throughout the file.
    """
    assert 'sequences: ["Ctrl+Shift+T"]' not in main_qml
    assert "controller.swap_to_terminal()" not in main_qml


def test_swap_chord_uses_application_shortcut_context(main_qml: str):
    """The editor↔terminal toggle chord must use Qt.ApplicationShortcut,
    not the default Qt.WindowShortcut — without that, NvimView's key
    handler captures the chord before it can fire. We assert a floor
    on total ApplicationShortcut blocks (anchor, FM toggle, this toggle,
    etc.) to catch a regression that silently demotes a chord."""
    assert main_qml.count("Qt.ApplicationShortcut") >= 3


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


def test_window_activation_considers_terminal_visible(main_qml: str):
    """Window.onActiveChanged must route to terminalView when the
    terminal is the current central surface. Without this, alt-tabbing
    back when terminal-visible leaves focus on a dead surface."""
    # Look for the specific dispatch branch.
    assert "controller.terminalVisible" in main_qml
    # The Window.onActiveChanged block should now contain a terminal branch.
    # Spread across multiple lines; we look for the terminalView focus call
    # alongside the activation dispatch.
    on_active_idx = main_qml.find("onActiveChanged:")
    assert on_active_idx >= 0
    # Body of onActiveChanged contains terminalView.forceActiveFocus.
    on_active_block = main_qml[on_active_idx : on_active_idx + 600]
    assert "terminalView.forceActiveFocus()" in on_active_block


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
    # so we don't get a false positive from the Ctrl+Shift+A handler
    # elsewhere in the file.
    header_idx = main_qml.find("id: locationHeader")
    assert header_idx >= 0
    # Header block extends until the next sibling — pick a generous
    # window that covers the Rectangle + its RowLayout children.
    header_block = main_qml[header_idx : header_idx + 2000]
    assert "controller.anchored" in header_block


# ---------------------------------------------------------------------------
# orchestrator.nvim chord relay — Ctrl+1..5 / Ctrl+Shift+Q → nvim RPC
# ---------------------------------------------------------------------------


def test_orchestrator_ctrl_digit_relay_present(main_qml: str):
    """An Instantiator with model:5 must exist and call
    `controller.send_editor_keys` for each Ctrl+1..5 chord.

    These chords are nvim-side orchestrator.nvim keymaps that the
    qmltermwidget fork cannot deliver (legacy VT key encoding has no
    byte representation for Ctrl+digit). Main.qml must intercept them at
    the Qt layer and inject the keycode over the RPC socket. Without this
    block the Ctrl+1..5 AgentsFocus keymaps are silently lost inside the
    embedded editor.
    """
    assert "Instantiator {" in main_qml
    assert "model: 5" in main_qml
    assert "controller.send_editor_keys(" in main_qml
    # The delegate must be gated on editorVisible so the relay fires only
    # when the editor is the active surface — not while shell/agent/FM is up.
    assert "enabled: controller.editorVisible" in main_qml


def test_orchestrator_ctrl_shift_q_relay_present(main_qml: str):
    """A standalone Shortcut for Ctrl+Shift+Q must call
    `controller.send_editor_keys` with the `<C-S-q>` keycode.

    This is orchestrator.nvim's AgentsClose chord — unencodable via the
    VT layer (Ctrl+Shift+letter collapses to Ctrl+letter in the 0x1f
    mask). Must be gated on `controller.editorVisible` — same rationale
    as Ctrl+1..5 above.
    """
    assert 'sequences: ["Ctrl+Shift+Q"]' in main_qml
    assert '"<C-S-q>"' in main_qml


def test_orchestrator_relay_gated_not_always_on(main_qml: str):
    """The relay chords must NOT be always-on (as the swap chords are).

    If Ctrl+1 were an ApplicationShortcut enabled unconditionally, it
    would fire while the shell pane is focused and inject a keycode into
    an unseen editor — silent, invisible state corruption. The `enabled:`
    binding to `controller.editorVisible` is the load-bearing gate.
    Verify the binding appears in the relay block, not just anywhere in
    the file.
    """
    # Find the Instantiator block and verify the gate appears within it.
    inst_idx = main_qml.find("Instantiator {")
    assert inst_idx >= 0
    # The block extends ~200 chars to the closing brace — enough to cover
    # the delegate's enabled property.
    relay_block = main_qml[inst_idx : inst_idx + 400]
    assert "controller.editorVisible" in relay_block


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
    assert 'sequences: ["Ctrl+Shift+V"]' in main_qml
    assert "pasteClipboard()" in main_qml


def test_paste_chord_gated_to_terminal_surfaces(main_qml: str):
    """The paste chord must be disabled while the agent/FM surface is up —
    Qt text inputs handle Ctrl+V natively, and pasting into an unseen
    terminal would be invisible state corruption (same rationale as the
    orchestrator relay gate)."""
    paste_idx = main_qml.find('sequences: ["Ctrl+Shift+V"]')
    assert paste_idx >= 0
    paste_block = main_qml[paste_idx : paste_idx + 400]
    assert "controller.editorVisible || controller.terminalVisible" in paste_block
