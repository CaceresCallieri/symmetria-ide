"""Tests for the embedded-browser pool on AppController (Stage 1).

The browser pool mirrors the terminal-agent pool, minus the
subprocess/argv/bridge machinery — a "window" is a QtWebEngine
WebEngineView the QML Loader owns; Python only tracks bookkeeping
(occupancy / dense display order / current title+url) and drives
open/focus/close as state flips the Loaders react to.

These are pure-Python tests: no QtWebEngine, no display. They pin the
pool's state machine + the central-surface integration. The actual
QtWebEngine-on-Wayland render check is a manual/subprocess smoke (the
session-scoped QCoreApplication fixture in conftest cannot coexist with
the QGuiApplication QtWebEngine requires), documented in the plan's
verification section.
"""

from __future__ import annotations

import os

import pytest

from symmetria_ide.app import AppController


@pytest.fixture
def controller():
    """Bare controller; shutdown on teardown (mirrors the central-surface fixture)."""
    ctrl = AppController()
    yield ctrl
    ctrl.shutdown()


def _capture(signal) -> list[None]:
    """Capture emission count for a parameterless signal."""
    emissions: list[None] = []
    signal.connect(lambda: emissions.append(None))
    return emissions


def _capture_int(signal) -> list[int]:
    """Capture the int argument of each emission of a 1-arg signal."""
    args: list[int] = []
    signal.connect(lambda v: args.append(v))
    return args


# ---------------------------------------------------------------------------
# Central-surface integration — "browser" is a valid surface, derived XOR.
# ---------------------------------------------------------------------------


def test_browser_is_a_valid_central_surface(controller):
    """set_central_surface accepts "browser" and the derived flag follows."""
    emissions = _capture(controller.centralSurfaceChanged)
    controller.set_central_surface("browser")
    assert controller.centralSurface == "browser"
    assert controller.browserSurfaceVisible is True
    assert controller.terminalVisible is False
    assert controller.editorVisible is False
    assert controller.gitVisible is False
    assert len(emissions) == 1


def test_browser_surface_visible_is_xor_with_terminal(controller):
    """browserSurfaceVisible derives from the same notify as the others —
    it can never be true at the same time as terminalVisible."""
    controller.set_central_surface("browser")
    assert controller.browserSurfaceVisible is not controller.terminalVisible


def test_unknown_surface_still_rejected(controller):
    """Adding "browser" to the validation tuple must not loosen it —
    a bogus value is still a no-op with no signal."""
    emissions = _capture(controller.centralSurfaceChanged)
    controller.set_central_surface("bogus")
    assert emissions == []
    assert controller.centralSurface == "terminal"


# ---------------------------------------------------------------------------
# Toggle slot — Ctrl+Shift+B, asymmetric ('B' names the browser).
# ---------------------------------------------------------------------------


def test_toggle_from_terminal_lands_on_browser(controller):
    assert controller.centralSurface == "terminal"
    emissions = _capture(controller.centralSurfaceChanged)
    controller.toggle_browser_terminal()
    assert controller.centralSurface == "browser"
    assert len(emissions) == 1


def test_toggle_from_browser_returns_to_terminal(controller):
    controller.swap_to_browser()
    emissions = _capture(controller.centralSurfaceChanged)
    controller.toggle_browser_terminal()
    assert controller.centralSurface == "terminal"
    assert len(emissions) == 1


def test_swap_to_browser_is_idempotent(controller):
    controller.swap_to_browser()
    emissions = _capture(controller.centralSurfaceChanged)
    controller.swap_to_browser()
    assert emissions == []
    assert controller.centralSurface == "browser"


# ---------------------------------------------------------------------------
# Pool list-shape invariants — fixed-length, indexed slot-1.
# ---------------------------------------------------------------------------


def test_pool_list_lengths_match_max_slots(controller):
    """browserSlotActive / browserTitles / browserUrls are stable-length
    (== maxBrowserSlots) so the fixed QML Repeater never churns delegates."""
    n = controller.maxBrowserSlots
    assert n == controller.maxAgentSlots  # both pools share _MAX_INSTANCES
    assert len(controller.browserSlotActive) == n
    assert len(controller.browserTitles) == n
    assert len(controller.browserUrls) == n
    assert controller.browserSlotActive == [False] * n
    assert controller.focusedBrowser == 0
    assert list(controller.browserOrder) == []


# ---------------------------------------------------------------------------
# open_browser — allocate, focus, switch surface.
# ---------------------------------------------------------------------------


def test_open_browser_allocates_focuses_and_switches_surface(controller):
    focus_args = _capture_int(controller.focusBrowserRequested)
    controller.open_browser()

    assert list(controller.browserOrder) == [1]
    assert controller.browserSlotActive[0] is True
    assert controller.browserUrls[0] == "about:blank"
    assert controller.focusedBrowser == 1
    # focus_browser auto-switches the central surface (chip/chord double
    # as a surface switcher — mirrors focus_agent).
    assert controller.centralSurface == "browser"
    assert focus_args == [1]


def test_open_browser_stores_explicit_url(controller):
    controller.open_browser("https://example.com")
    assert controller.browserUrls[0] == "https://example.com"


def test_open_browser_fills_from_bottom_and_appends_display_order(controller):
    controller.open_browser()  # slot 1
    controller.open_browser()  # slot 2
    controller.open_browser()  # slot 3
    assert list(controller.browserOrder) == [1, 2, 3]
    assert controller.browserSlotActive == [True, True, True, False, False]


def test_open_browser_pool_full_is_noop(controller):
    for _ in range(controller.maxBrowserSlots):
        controller.open_browser()
    assert len(controller.browserOrder) == controller.maxBrowserSlots
    emissions = _capture(controller.browserTabsChanged)
    controller.open_browser()  # one past full
    assert emissions == []
    assert len(controller.browserOrder) == controller.maxBrowserSlots


# ---------------------------------------------------------------------------
# focus_browser / cycle_browser_focus.
# ---------------------------------------------------------------------------


def test_focus_browser_empty_slot_is_noop(controller):
    controller.set_central_surface("editor")
    emissions = _capture(controller.focusedBrowserChanged)
    controller.focus_browser(3)  # nothing in slot 3
    assert emissions == []
    assert controller.focusedBrowser == 0
    # No spurious surface switch on an empty-slot focus.
    assert controller.centralSurface == "editor"


def test_cycle_browser_focus_wraps_in_display_order(controller):
    controller.open_browser()  # slot 1
    controller.open_browser()  # slot 2
    controller.open_browser()  # slot 3 (focused, newest)
    assert controller.focusedBrowser == 3

    controller.cycle_browser_focus(+1)  # wraps 3 → 1
    assert controller.focusedBrowser == 1
    controller.cycle_browser_focus(-1)  # 1 → 3
    assert controller.focusedBrowser == 3
    controller.cycle_browser_focus(-1)  # 3 → 2
    assert controller.focusedBrowser == 2


# ---------------------------------------------------------------------------
# close_browser — compaction, refocus, last-close fallback to terminal.
# ---------------------------------------------------------------------------


def test_close_browser_compacts_order_and_refocuses_previous(controller):
    controller.open_browser()  # slot 1
    controller.open_browser()  # slot 2
    controller.open_browser()  # slot 3 (focused)
    controller.focus_browser(2)  # focus the middle window

    controller.close_browser(2)

    # Internal slot 2 frees; display order compacts to [1, 3].
    assert list(controller.browserOrder) == [1, 3]
    assert controller.browserSlotActive == [True, False, True, False, False]
    # Refocus walks to the PREVIOUS display position (slot 1).
    assert controller.focusedBrowser == 1


def test_close_last_browser_falls_back_to_terminal(controller):
    controller.open_browser()  # slot 1, surface == browser
    assert controller.centralSurface == "browser"

    controller.close_browser(1)

    assert list(controller.browserOrder) == []
    assert controller.focusedBrowser == 0
    # Closing the last window returns to the home surface.
    assert controller.centralSurface == "terminal"


def test_close_focused_browser_targets_focused_slot(controller):
    controller.open_browser()  # slot 1
    controller.open_browser()  # slot 2 (focused)
    controller.close_focused_browser()
    assert list(controller.browserOrder) == [1]


def test_reopen_after_close_fills_freed_slot(controller):
    controller.open_browser()  # 1
    controller.open_browser()  # 2
    controller.close_browser(1)  # frees internal slot 1
    controller.open_browser()  # should reuse slot 1 (lowest free)
    # Internal slot reused (fill-from-bottom), but APPENDED in display order.
    assert sorted(controller.browserOrder) == [1, 2]
    assert list(controller.browserOrder) == [2, 1]


# ---------------------------------------------------------------------------
# Defensive branches — mirror the agent pool's recovery paths (silent rot
# risk: these only fire on a desync/double-close race, so they need a test).
# ---------------------------------------------------------------------------


def test_close_browser_desync_removes_from_tabs_without_raising(controller):
    """A slot present in _browser_tabs but missing from _browser_order
    (a double-close race) must NOT raise on order.index() — close_browser
    drops it from tabs and emits once, mirroring close_agent's guard."""
    controller.open_browser()  # slot 1, properly tracked
    # Force a desync: add slot 2 to tabs only (not to the order list).
    controller._browser_tabs[2] = {"url": "about:blank", "title": ""}
    emissions = _capture(controller.browserTabsChanged)
    controller.close_browser(2)  # must not raise
    assert 2 not in controller._browser_tabs
    assert len(emissions) == 1
    # The properly-tracked slot 1 is untouched.
    assert list(controller.browserOrder) == [1]


def test_cycle_browser_focus_recovers_from_stale_focus(controller):
    """If _focused_browser points at a slot no longer in display order
    (stale state), cycle_browser_focus recovers to the first window via the
    `order[0]` path rather than raising on order.index()."""
    controller.open_browser()  # slot 1
    controller.open_browser()  # slot 2
    controller._focused_browser = 99  # stale — not in _browser_order
    controller.cycle_browser_focus(1)
    assert controller.focusedBrowser == controller.browserOrder[0]


# ---------------------------------------------------------------------------
# Title / URL bookkeeping — dedup, no spurious emits.
# ---------------------------------------------------------------------------


def test_on_browser_title_updates_and_dedups(controller):
    controller.open_browser()
    emissions = _capture(controller.browserTabsChanged)
    controller.on_browser_title(1, "Example Domain")
    assert controller.browserTitles[0] == "Example Domain"
    assert len(emissions) == 1
    controller.on_browser_title(1, "Example Domain")  # identical → no emit
    assert len(emissions) == 1


def test_on_browser_url_updates_and_dedups(controller):
    controller.open_browser("about:blank")
    emissions = _capture(controller.browserTabsChanged)
    controller.on_browser_url(1, "https://example.com/")
    assert controller.browserUrls[0] == "https://example.com/"
    assert len(emissions) == 1
    controller.on_browser_url(1, "https://example.com/")  # identical → no emit
    assert len(emissions) == 1


def test_title_and_url_callbacks_ignore_empty_slots(controller):
    """Callbacks for a slot with no window are silent no-ops (the QML
    delegate and Python bookkeeping can briefly disagree around teardown)."""
    controller.on_browser_title(4, "ghost")  # no window in slot 4
    controller.on_browser_url(4, "https://ghost")
    assert controller.browserTitles[3] == ""
    assert controller.browserUrls[3] == ""


# ---------------------------------------------------------------------------
# MCP window addressing (Stage 2b) — display position ↔ internal slot.
# ---------------------------------------------------------------------------


def test_resolve_browser_window_maps_display_position_to_slot(controller):
    controller.open_browser()  # slot 1
    controller.open_browser()  # slot 2 (focused)
    controller.close_browser(1)  # free slot 1; order now [2]
    controller.open_browser()  # reuses internal slot 1; order [2, 1]
    # window 0 = focused (internal slot 1, the newest).
    assert controller._resolve_browser_window(0) == controller.focusedBrowser
    # display positions map through _browser_order, not internal slot numbers.
    assert controller._resolve_browser_window(1) == 2  # 1st displayed → slot 2
    assert controller._resolve_browser_window(2) == 1  # 2nd displayed → slot 1
    assert controller._resolve_browser_window(3) == 0  # out of range → no window


def test_resolve_browser_window_empty_pool(controller):
    assert controller._resolve_browser_window(0) == 0  # focused == 0
    assert controller._resolve_browser_window(1) == 0


def test_read_browser_windows_reports_display_positions(controller):
    controller.open_browser("https://a.com")  # slot 1, display 1
    controller.on_browser_title(1, "A")
    controller.open_browser("https://b.com")  # slot 2, display 2 (focused)
    info = controller._read_browser_windows()
    assert info["ok"] is True
    assert info["focused"] == 2  # display position, not internal slot
    assert info["windows"][0] == {"window": 1, "title": "A", "url": "https://a.com"}
    assert info["windows"][1] == {"window": 2, "title": "", "url": "https://b.com"}


def test_open_browser_for_mcp_returns_display_number(controller):
    assert controller._open_browser_for_mcp("https://x.com") == {
        "ok": True,
        "window": 1,
    }
    assert controller.browserUrls[0] == "https://x.com"
    # Second open appends as display position 2.
    assert controller._open_browser_for_mcp("about:blank") == {"ok": True, "window": 2}


def test_open_browser_for_mcp_pool_full(controller):
    for _ in range(controller.maxBrowserSlots):
        controller._open_browser_for_mcp("about:blank")
    result = controller._open_browser_for_mcp("about:blank")
    assert result["ok"] is False
    assert result["error"] == "pool-full"


# ---------------------------------------------------------------------------
# Agent ↔ browser attribution (Stage 3) — the chip browser glyph's data.
# `<our_pid>_<slot>` ids map to a chip slot; the count/active lists are
# indexed slot-1 (agent slot 2 → index 1), mirroring agentTitles.
# ---------------------------------------------------------------------------


def test_attribution_records_ownership_and_pulse(controller):
    aid = f"{os.getpid()}_2"  # agent slot 2 → index 1
    controller.open_browser()  # browser slot 1
    emissions = _capture(controller.agentBrowserChanged)

    controller._record_browser_attribution(aid, 1, "start")
    assert controller.agentBrowserCount[1] == 1
    assert controller.agentBrowserActive[1] is True  # in-flight → pulse

    controller._record_browser_attribution(aid, 1, "end")
    assert controller.agentBrowserActive[1] is False  # op done → no pulse
    assert controller.agentBrowserCount[1] == 1  # ownership persists
    assert len(emissions) == 2


def test_attribution_ignores_foreign_and_malformed_ids(controller):
    controller.open_browser()
    controller._record_browser_attribution("", 1, "start")  # untagged
    controller._record_browser_attribution(
        f"{os.getpid() + 1}_2", 1, "start"
    )  # foreign pid
    controller._record_browser_attribution("garbage", 1, "start")  # malformed
    assert controller.agentBrowserCount == [0] * controller.maxAgentSlots
    assert controller.agentBrowserActive == [False] * controller.maxAgentSlots


def test_focus_agent_browser_jumps_to_newest_owned_window(controller):
    aid = f"{os.getpid()}_1"  # agent slot 1
    controller.open_browser()  # browser slot 1
    controller.open_browser()  # browser slot 2
    controller._record_browser_attribution(aid, 1, "start")
    controller._record_browser_attribution(aid, 2, "start")  # newest-driven = 2
    controller.set_central_surface("editor")
    focus_args = _capture_int(controller.focusBrowserRequested)

    controller.focus_agent_browser(1)

    assert controller.focusedBrowser == 2  # newest owned window
    assert controller.centralSurface == "browser"  # focus_browser switches surface
    assert focus_args == [2]


def test_redriving_a_window_makes_it_the_newest_jump_target(controller):
    aid = f"{os.getpid()}_1"
    controller.open_browser()  # slot 1
    controller.open_browser()  # slot 2
    controller._record_browser_attribution(aid, 1, "start")
    controller._record_browser_attribution(aid, 2, "start")
    controller._record_browser_attribution(aid, 1, "start")  # re-drive 1 → newest

    controller.focus_agent_browser(1)
    assert controller.focusedBrowser == 1
    assert controller.agentBrowserCount[0] == 2  # owns both, no duplicate entry


def test_focus_agent_browser_noop_without_owned_window(controller):
    controller.set_central_surface("editor")
    emissions = _capture_int(controller.focusBrowserRequested)
    controller.focus_agent_browser(3)  # agent 3 owns nothing
    assert emissions == []
    assert controller.centralSurface == "editor"  # no spurious surface switch


def test_close_browser_prunes_agent_ownership(controller):
    aid = f"{os.getpid()}_1"
    controller.open_browser()  # browser slot 1
    controller._record_browser_attribution(aid, 1, "start")
    assert controller.agentBrowserCount[0] == 1

    controller.close_browser(1)
    assert controller.agentBrowserCount[0] == 0  # closed window → link gone


def test_open_browser_for_mcp_attributes_window_to_caller(controller):
    aid = f"{os.getpid()}_3"  # agent slot 3 → index 2
    result = controller._open_browser_for_mcp("https://x.com", aid)
    assert result == {"ok": True, "window": 1}
    assert controller.agentBrowserCount[2] == 1  # caller owns the opened window
    assert controller.agentBrowserActive[2] is False  # open's start/end nets to idle

    controller.set_central_surface("editor")
    controller.focus_agent_browser(3)
    assert controller.focusedBrowser == 1  # the opened window is the jump target


def test_close_agent_prunes_browser_links(controller):
    """Closing an agent drops its browser ownership/activity so a freed slot
    doesn't carry a stale link into a future agent reusing the slot."""
    aid = f"{os.getpid()}_2"
    controller.open_browser()  # browser slot 1
    controller._record_browser_attribution(aid, 1, "start")
    assert controller.agentBrowserCount[1] == 1
    # Minimal agent state so close_agent's normal (non-desync) path runs.
    controller._term_agents[2] = {"harness": "claude", "title": ""}
    controller._agent_order = [2]

    controller.close_agent(2)
    assert controller.agentBrowserCount[1] == 0  # links pruned on agent close
