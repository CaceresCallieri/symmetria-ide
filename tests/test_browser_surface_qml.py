"""Structural smoke tests for the embedded-browser surface QML wiring.

QML has no hermetic unit surface here (instantiating it needs a full
QGuiApplication + window, and QtWebEngine needs GPU/display), so — like
test_main_qml_terminal_wiring.py — we read the files and assert the
load-bearing structural pieces are present. These catch regressions a
"refactor the QML" PR could silently introduce: the fixed Repeater
swapped for a list-model (which would destroy live WebEngineViews), a
missing chord, the surface dropped from the switcher, or the persistent
profile turned off-the-record (losing logins on restart).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_QML = Path(__file__).resolve().parent.parent / "qml"


@pytest.fixture(scope="module")
def browser_surface() -> str:
    return (_QML / "BrowserSurface.qml").read_text()


@pytest.fixture(scope="module")
def main_qml() -> str:
    return (_QML / "Main.qml").read_text()


@pytest.fixture(scope="module")
def agent_top_bar() -> str:
    return (_QML / "AgentTopBar.qml").read_text()


# ---------------------------------------------------------------------------
# BrowserSurface.qml — embedding + the load-bearing pool structure.
# ---------------------------------------------------------------------------


def test_imports_qtwebengine(browser_surface: str):
    assert "import QtWebEngine" in browser_surface


def test_embeds_webengineview(browser_surface: str):
    """The browser is an embedded view, not a spawned window — that is the
    whole containment mechanism."""
    assert "WebEngineView" in browser_surface


def test_persistent_profile_for_logins(browser_surface: str):
    """A named, persistent (non-off-the-record) profile so cookies/logins
    survive restart. storageName alone leaves offTheRecord=true (not
    persistent), so offTheRecord must also be set false — but AFTER
    storageName, via Component.onCompleted, to avoid QtWebEngine's
    "Storage name is empty" warning (see the regression note in the QML).
    Pin both, and pin the deferred (not inline) form."""
    assert "WebEngineProfile" in browser_surface
    assert 'storageName: "symmetria-ide"' in browser_surface
    # Pin the robust DEFERRED form exactly — offTheRecord set after
    # storageName (at completion), which is warning-free and persistent.
    # The inline `storageName; offTheRecord: false` form warns on QtWebEngine
    # 6.11 (see the regression note in the QML); pinning the exact deferred
    # line guards against a refactor flattening it back.
    assert "Component.onCompleted: offTheRecord = false" in browser_surface


def test_viewport_uses_fixed_repeater_not_list_model(browser_surface: str):
    """The viewport Repeater MUST be the fixed `maxBrowserSlots` model with
    Loaders gated on `browserSlotActive` — a list-model over `browserOrder`
    would churn delegates and destroy live WebEngineViews on open/close.
    The tab strip legitimately uses browserOrder; the VIEW pool must not."""
    assert "model: controller.maxBrowserSlots" in browser_surface
    assert "active: controller.browserSlotActive[slotLoader.index]" in browser_surface
    # The cautionary comment must survive refactors (mirrors the agent surface).
    assert "list-model" in browser_surface.lower()


def test_title_and_url_callbacks_wired(browser_surface: str):
    assert "controller.on_browser_title(" in browser_surface
    assert "controller.on_browser_url(" in browser_surface


def test_open_close_focus_affordances_wired(browser_surface: str):
    assert "controller.open_browser()" in browser_surface
    assert "controller.close_browser(" in browser_surface
    assert "controller.focus_browser(" in browser_surface


def test_focus_pull_connection_present(browser_surface: str):
    """The already-visible focus-pull mirrors the agent delegate so a
    chip/chord lands keyboard focus on the page."""
    assert "onFocusBrowserRequested" in browser_surface


# ---------------------------------------------------------------------------
# Main.qml — chord + surface instantiation.
# ---------------------------------------------------------------------------


def test_browser_toggle_chord_exists(main_qml: str):
    assert "Ctrl+Shift+B" in main_qml
    assert "controller.toggle_browser_terminal()" in main_qml


def test_browser_surface_instantiated_and_gated(main_qml: str):
    assert "BrowserSurface {" in main_qml
    assert "controller.browserSurfaceVisible" in main_qml


# ---------------------------------------------------------------------------
# AgentTopBar.qml — the always-visible surface switcher includes browser.
# ---------------------------------------------------------------------------


def test_switcher_includes_browser_surface(agent_top_bar: str):
    assert 'surface: "browser"' in agent_top_bar
