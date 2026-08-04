"""Structural tests for Main.qml's nested-compositor browser wiring.

Same pattern and rationale as `test_main_qml_terminal_wiring.py`: QML has no
hermetic unit-test surface here, so the load-bearing structure is asserted by
reading the file.

The invariant these exist for is the most dangerous one this surface has.
`browserPaneLoader` hosts the nested Wayland compositor, which owns Chrome's
Wayland CONNECTION — so binding its `active` to visibility (the obvious
"optimisation", and what every other optional surface in this file does) would
kill the browser process's display on the first surface swap. Nothing in the
Python suite can see that: it is one QML property away at all times.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from symmetria_ide import chrome_host


@pytest.fixture(scope="module")
def main_qml() -> str:
    repo_root = Path(__file__).resolve().parent.parent
    return (repo_root / "qml" / "Main.qml").read_text()


@pytest.fixture(scope="module")
def loader_block(main_qml: str) -> str:
    """The `Loader { id: browserPaneLoader ... }` body."""
    start = main_qml.index("id: browserPaneLoader")
    # Far enough to cover the block; the assertions below are all specific
    # enough that trailing siblings cannot satisfy them by accident.
    return main_qml[start : start + 1200]


class TestNeverDeactivated:
    def test_loader_is_unconditionally_active(self, loader_block: str):
        assert re.search(r"\bactive:\s*true\b", loader_block)

    def test_active_is_never_bound_to_visibility(self, loader_block: str):
        """The regression this whole file exists for: `active: <something>
        Visible` unloads the compositor on a surface swap and takes Chrome's
        Wayland connection with it."""
        match = re.search(r"\bactive:\s*(.+)", loader_block)
        assert match, "browserPaneLoader has no `active` binding at all"
        assert "Visible" not in match.group(1)
        assert "visible" not in match.group(1)

    def test_visibility_is_gated_separately(self, loader_block: str):
        """Hiding is the correct gate — and free (measured: full frame rate
        and ~60ms screenshots while hidden AND off-workspace)."""
        assert "controller.browserVisible" in loader_block
        assert re.search(r"\bvisible:", loader_block)


class TestRequiredProperties:
    def test_pane_is_constructed_with_its_required_properties(self, loader_block: str):
        """They are `required`, so they can only be supplied at construction —
        `setSource` is what makes that possible, and assigning them in
        `onLoaded` instead fails the load outright."""
        assert "setSource(" in loader_block
        assert '"hostWindow"' in loader_block
        assert '"socketName"' in loader_block

    def test_socket_name_comes_from_the_context_property(self, loader_block: str):
        assert "browserWaylandSocket" in loader_block


class TestSocketContract:
    def test_context_property_matches_the_hosts_identity(self):
        """The compositor listens on this name and `ChromeHost` points Chrome
        at it via WAYLAND_DISPLAY. If the two ever derive it differently,
        Chrome connects to nothing — or, worse, to the host compositor."""
        repo_root = Path(__file__).resolve().parent.parent
        app_py = (repo_root / "src" / "symmetria_ide" / "app.py").read_text()
        assert (
            'setContextProperty("browserWaylandSocket", browser_identity(os.getpid()))'
            in app_py
        )
        assert chrome_host.browser_identity(os.getpid()).startswith(
            "symmetria-browser-"
        )


class TestChords:
    def test_browser_toggle_is_bound(self, main_qml: str):
        assert "controller.toggle_browser()" in main_qml

    def test_window_cycling_is_scoped_to_the_browser_surface(self, main_qml: str):
        """The agent surface binds the SAME chords. Qt only arbitrates between
        ENABLED shortcuts, so the two pairs must stay disjoint by `enabled` —
        drop the guard and both fire, or neither does."""
        for sequence in ("Ctrl+Shift+L", "Ctrl+Shift+H"):
            blocks = [
                main_qml[m.start() : m.start() + 400]
                for m in re.finditer(re.escape(f'sequences: ["{sequence}"]'), main_qml)
            ]
            assert len(blocks) == 2, f"expected agent + browser bindings for {sequence}"
            guards = {
                "browser" if "browserVisible" in b else "agent"
                for b in blocks
                if "enabled:" in b
            }
            assert guards == {"agent", "browser"}

    def test_focus_restore_covers_the_browser_surface(self, main_qml: str):
        """Without this branch, re-activating the window while the browser is
        central sends focus to the hidden nvim pane and keystrokes vanish."""
        assert "controller.browserVisible && browserPaneLoader.item" in main_qml
        assert "browserPaneLoader.item.activateCurrent()" in main_qml
