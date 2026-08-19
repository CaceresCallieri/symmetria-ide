"""Tests for the browser window registry on AppController.

The agentic browser is real Google Chrome running as its own process
(`chrome_host.py`), so a "window" here is a Chrome window pinned by a Hyprland
rule to the IDE's workspace — not an embedded view. What AppController still
owns is the REGISTRY: slot allocation, dense display numbering, agent
ownership, and the attention badge. That registry is what the MCP tools number
and what the session manifest persists, which is why it survived the migration
intact while the surface around it did not.

Pure-Python: no browser, no compositor. `conftest._isolate_browser_host` makes
spawning impossible even by accident, and these tests inject a FakeChromeHost.
"""

from __future__ import annotations

import os

import pytest
from conftest import FakeChromeHost

from symmetria_ide.app import AppController


@pytest.fixture
def controller(monkeypatch):
    """Controller whose browser is a FakeChromeHost.

    Patched at the module symbol because the controller builds its host
    lazily, on first browser use.
    """
    monkeypatch.setattr("symmetria_ide.app.ChromeHost", FakeChromeHost)
    ctrl = AppController()
    # Two agent slots, registered directly rather than spawned (a spawn would
    # drag in focus, bridge publication and surface switching, none of which
    # this file is about). They must exist: a browser window is OWNED by an
    # agent, and since 2026-08-15 the per-agent-slot lists below —
    # agentBrowserCount, agentBrowserAttention — are sized by the pane
    # registry's high-water mark rather than by a fixed five, so a link
    # attributed to a slot no agent ever held would have nowhere to show.
    for slot in (1, 2):
        ctrl._term_agents[slot] = {
            "harness": "claude",
            "cwd": "",
            "title": "",
            "dangerous": True,
        }
        ctrl._agent_order.append(slot)
    ctrl._sync_pane_slots()
    yield ctrl
    ctrl.shutdown()


def chrome(controller) -> FakeChromeHost:
    """The controller's (lazily built) fake host."""
    return controller._ensure_chrome_host()


def _capture(signal) -> list[None]:
    emissions: list[None] = []
    signal.connect(lambda: emissions.append(None))
    return emissions


def _agent_id(slot: int) -> str:
    return f"{os.getpid()}_{slot}"


# ---------------------------------------------------------------------------
# The browser is no longer a central surface
# ---------------------------------------------------------------------------


def test_browser_is_a_central_surface_again(controller):
    """ "browser" was valid under the embedded QtWebEngine pool, rejected while
    Chrome was an external Hyprland window (nothing to show in-window), and is
    valid again now that Chrome renders in our nested compositor."""
    emissions = _capture(controller.centralSurfaceChanged)
    controller.set_central_surface("browser")
    assert emissions
    assert controller.centralSurface == "browser"


def test_bogus_surfaces_are_still_rejected(controller):
    emissions = _capture(controller.centralSurfaceChanged)
    controller.set_central_surface("nonsense")
    assert emissions == []
    assert controller.centralSurface == "terminal"


def test_real_surfaces_still_work(controller):
    controller.set_central_surface("git")
    assert controller.gitVisible is True


# ---------------------------------------------------------------------------
# Slot allocation
# ---------------------------------------------------------------------------


def test_open_browser_asks_chrome_and_registers_a_slot(controller):
    assert controller.open_browser("https://example.com") == ""
    assert chrome(controller).opened == ["https://example.com"]
    assert controller._browser_order == [1]
    assert controller._browser_tabs[1]["url"] == "https://example.com"


def test_open_browser_fills_from_bottom_and_appends_display_order(controller):
    for _ in range(3):
        controller.open_browser()
    assert controller._browser_order == [1, 2, 3]


def test_reopen_after_close_fills_the_freed_slot(controller):
    for _ in range(3):
        controller.open_browser()
    controller.close_browser(2)
    controller.open_browser()
    # Slot 2 is reused, but it APPENDS in display order — display numbering is
    # positional, internal slots are not.
    assert controller._browser_order == [1, 3, 2]


def test_pool_full_is_reported_not_silently_dropped(controller):
    for _ in range(controller._MAX_INSTANCES):
        controller.open_browser()
    assert controller.open_browser() == "pool-full"
    assert len(controller._browser_order) == controller._MAX_INSTANCES


def test_chrome_failure_does_not_register_a_phantom_slot(controller):
    """A slot registered for a window that was never created would show up in
    browser_list_windows and be attributed to an agent — a lie the agent then
    acts on."""
    chrome(controller).open_error = "chrome-not-installed"
    assert controller.open_browser() == "chrome-not-installed"
    assert controller._browser_order == []


def test_open_emits_one_registry_notify(controller):
    emissions = _capture(controller.browserTabsChanged)
    controller.open_browser()
    assert len(emissions) == 1


# ---------------------------------------------------------------------------
# CDP target binding + live title/url
# ---------------------------------------------------------------------------


def test_target_id_binds_when_chrome_answers(controller):
    controller.open_browser("https://a.test")
    chrome(controller).complete_open("T-1")
    assert controller._browser_tabs[1]["target"] == "T-1"


def test_target_updates_refresh_title_and_url(controller):
    controller.open_browser("https://a.test")
    chrome(controller).complete_open("T-1")
    controller._on_chrome_window_updated("T-1", "https://a.test/page", "A Page")
    assert controller._browser_tabs[1]["title"] == "A Page"
    assert controller._browser_tabs[1]["url"] == "https://a.test/page"


def test_duplicate_target_update_does_not_re_emit(controller):
    controller.open_browser("https://a.test")
    chrome(controller).complete_open("T-1")
    controller._on_chrome_window_updated("T-1", "u", "t")
    emissions = _capture(controller.browserTabsChanged)
    controller._on_chrome_window_updated("T-1", "u", "t")
    assert emissions == []


def test_unbound_slot_adopts_a_discovered_target(controller):
    """The `--new-window` fallback (CDP not attached yet) creates a window we
    never get a targetId for. Discovery is how we learn it — without adoption
    that window would never update its title and could never be closed."""
    controller.open_browser("https://a.test")
    assert controller._browser_tabs[1]["target"] == ""
    controller._on_chrome_window_updated("T-9", "https://a.test", "A")
    assert controller._browser_tabs[1]["target"] == "T-9"


def test_chrome_internal_pages_are_never_adopted(controller):
    """Chrome opens a new-tab page in situations we don't control. Adopting
    one would point the slot at a window the agent never asked for, leaving
    the real window unattributed and un-closable. Observed live: on a cold
    start the newtab target was discovered FIRST, ahead of the real page."""
    controller.open_browser("https://a.test")
    controller._on_chrome_window_updated("T-NEWTAB", "chrome://newtab/", "New Tab")
    assert controller._browser_tabs[1]["target"] == ""
    controller._on_chrome_window_updated("T-REAL", "https://a.test", "A")
    assert controller._browser_tabs[1]["target"] == "T-REAL"


def test_targets_with_no_waiting_slot_are_ignored(controller):
    """The user's own windows/tabs are not ours to pool."""
    controller.open_browser()
    chrome(controller).complete_open("T-1")
    controller._on_chrome_window_updated("USER-TAB", "https://x.test", "X")
    assert list(controller._browser_tabs) == [1]
    assert controller._browser_tabs[1]["target"] == "T-1"


def test_user_closing_a_window_drops_its_slot(controller):
    controller.open_browser()
    chrome(controller).complete_open("T-1")
    controller._on_chrome_window_gone("T-1")
    assert controller._browser_order == []


def test_unknown_target_gone_is_inert(controller):
    controller.open_browser()
    chrome(controller).complete_open("T-1")
    controller._on_chrome_window_gone("SOMEONE-ELSE")
    assert controller._browser_order == [1]


# ---------------------------------------------------------------------------
# MCP tool bodies
# ---------------------------------------------------------------------------


def test_read_browser_windows_reports_display_positions(controller):
    for url in ("https://a.test", "https://b.test"):
        controller.open_browser(url)
    controller.close_browser(1)
    result = controller._read_browser_windows()
    assert result["ok"] is True
    assert [w["window"] for w in result["windows"]] == [1]
    assert result["windows"][0]["url"] == "https://b.test"


def test_read_browser_windows_has_no_focus_field(controller):
    """ "Which window is focused" is a question about the user's compositor
    now, not about IDE state — a constant 0 would be a lie dressed as data."""
    controller.open_browser()
    assert "focused" not in controller._read_browser_windows()


def test_open_for_mcp_returns_display_number(controller):
    result = controller._open_browser_for_mcp("https://a.test")
    assert result["ok"] is True
    assert result["window"] == 1
    assert result["url"] == "https://a.test"


def test_open_for_mcp_surfaces_chrome_failure_to_the_agent(controller):
    chrome(controller).open_error = "chrome-not-installed"
    result = controller._open_browser_for_mcp("https://a.test")
    assert result == {"ok": False, "error": "chrome-not-installed"}


def test_open_for_mcp_pool_full(controller):
    for _ in range(controller._MAX_INSTANCES):
        controller.open_browser()
    assert controller._open_browser_for_mcp("https://a.test") == {
        "ok": False,
        "error": "pool-full",
    }


def test_open_for_mcp_never_switches_surface(controller):
    """An agent browsing must not move the user. With Chrome external the
    window maps silently on the IDE's workspace via the pin rule."""
    controller.set_central_surface("editor")
    controller._open_browser_for_mcp("https://a.test")
    assert controller.centralSurface == "editor"


# ---------------------------------------------------------------------------
# Agent ownership + attention
# ---------------------------------------------------------------------------


def test_open_for_mcp_attributes_the_window_to_its_caller(controller):
    controller._open_browser_for_mcp("https://a.test", _agent_id(2))
    assert controller._agent_browser_windows[2] == [1]
    assert controller.agentBrowserCount[1] == 1


def test_foreign_and_malformed_ids_never_accrue_links(controller):
    for bad in ("999999_1", "garbage", "", "abc_x"):
        controller._open_browser_for_mcp("https://a.test", bad)
    assert controller._agent_browser_windows == {}


def test_newest_window_is_last(controller):
    controller._open_browser_for_mcp("https://a.test", _agent_id(1))
    controller._open_browser_for_mcp("https://b.test", _agent_id(1))
    assert controller._agent_browser_windows[1] == [1, 2]


def test_closing_a_window_prunes_ownership(controller):
    controller._open_browser_for_mcp("https://a.test", _agent_id(1))
    controller.close_browser(1)
    assert controller._agent_browser_windows[1] == []
    assert controller.agentBrowserCount[0] == 0


def test_request_attention_lights_the_dot(controller):
    controller._open_browser_for_mcp("https://a.test", _agent_id(1))
    assert controller._set_browser_attention_for_mcp(_agent_id(1), "look") == {
        "ok": True
    }
    assert controller.agentBrowserAttention[0] is True


def test_request_attention_requires_an_owned_window(controller):
    """The dot rides the globe, which only renders at count > 0 — storing an
    attention the user can never see would be a silently dropped signal."""
    assert controller._set_browser_attention_for_mcp(_agent_id(1)) == {
        "ok": False,
        "error": "no-window",
    }


def test_request_attention_rejects_foreign_callers(controller):
    assert controller._set_browser_attention_for_mcp("999999_1") == {
        "ok": False,
        "error": "unknown-agent",
    }


def test_checking_in_clears_the_attention_dot(controller):
    controller._open_browser_for_mcp("https://a.test", _agent_id(1))
    controller._set_browser_attention_for_mcp(_agent_id(1), "look")
    controller.focus_agent_browser(1)
    assert controller.agentBrowserAttention[0] is False


def test_closing_the_last_window_clears_attention(controller):
    """A lingering entry would wrongly re-light the dot if the agent later
    opens a fresh window."""
    controller._open_browser_for_mcp("https://a.test", _agent_id(1))
    controller._set_browser_attention_for_mcp(_agent_id(1), "look")
    controller.close_browser(1)
    assert controller.agentBrowserAttention[0] is False


def test_attention_survives_while_any_window_remains(controller):
    controller._open_browser_for_mcp("https://a.test", _agent_id(1))
    controller._open_browser_for_mcp("https://b.test", _agent_id(1))
    controller._set_browser_attention_for_mcp(_agent_id(1), "look")
    controller.close_browser(1)
    assert controller.agentBrowserAttention[0] is True


# ---------------------------------------------------------------------------
# Browser surface navigation (Ctrl+Shift+B / chip globe)
# ---------------------------------------------------------------------------


def test_chord_navigates_to_the_browser(controller):
    """Navigation again, after a spell as a notify-only chord. That exception
    existed only because the external-Chrome backend made the browser a
    Hyprland window, so "going there" meant a workspace switch the user never
    asked for. In-window, it is an ordinary surface swap."""
    controller._open_browser_for_mcp("https://a.test", _agent_id(1))
    controller.toggle_browser()
    assert controller.centralSurface == "browser"
    assert controller.browserVisible is True


def test_chord_swaps_back_to_where_you_came_from(controller):
    """Swap-last-two, like Ctrl+Shift+E/T/G — not a hard-coded return to the
    terminal."""
    controller._open_browser_for_mcp("https://a.test", _agent_id(1))
    controller.set_central_surface("editor")
    controller.toggle_browser()
    controller.toggle_browser()
    assert controller.centralSurface == "editor"


def test_arriving_clears_the_agents_attention_dot(controller):
    """The agent asked to be looked at; the user has now looked."""
    controller._open_browser_for_mcp("https://a.test", _agent_id(1))
    controller._set_browser_attention_for_mcp(_agent_id(1), "the deploy failed")
    controller._focused_term_agent = 1
    assert controller.agentBrowserAttention[0] is True

    controller.toggle_browser()

    assert controller.agentBrowserAttention[0] is False


def test_globe_click_is_one_way(controller):
    """Clicking a SPECIFIC agent's globe means "show me that", so unlike the
    chord it never swaps back."""
    controller._open_browser_for_mcp("https://a.test", _agent_id(1))
    controller.focus_agent_browser(1)
    controller.focus_agent_browser(1)
    assert controller.centralSurface == "browser"


def test_globe_click_without_a_window_is_a_no_op(controller):
    surface_before = controller.centralSurface
    controller.focus_agent_browser(1)
    assert controller.centralSurface == surface_before


def test_an_unused_browser_is_not_a_back_target(controller):
    """Returning "back" to a browser that never opened a window is a dead end
    — a blank pane — so the back-target falls through to the terminal."""
    assert controller._surface_is_navigable("browser") is False
    controller._open_browser_for_mcp("https://a.test", _agent_id(1))
    assert controller._surface_is_navigable("browser") is True


# ---------------------------------------------------------------------------
# Agent death
# ---------------------------------------------------------------------------


def test_dead_agents_solely_owned_window_is_closed_in_chrome(controller):
    """Not just unlinked: an orphan Chrome window would sit on the workspace
    with nothing in the IDE referring to it."""
    controller._open_browser_for_mcp("https://a.test", _agent_id(1))
    chrome(controller).complete_open("T-1")
    controller._release_agent_browser_windows(1)
    assert chrome(controller).closed == ["T-1"]
    assert controller._browser_order == []


def test_window_co_owned_by_a_living_agent_survives(controller):
    controller._open_browser_for_mcp("https://a.test", _agent_id(1))
    chrome(controller).complete_open("T-1")
    controller._claim_browser_window(2, 1)
    controller._release_agent_browser_windows(1)
    assert chrome(controller).closed == []
    assert controller._browser_order == [1]
    assert controller._agent_browser_windows[2] == [1]


def test_agent_death_drops_its_links_and_attention(controller):
    controller._open_browser_for_mcp("https://a.test", _agent_id(1))
    controller._set_browser_attention_for_mcp(_agent_id(1), "look")
    controller._release_agent_browser_windows(1)
    assert 1 not in controller._agent_browser_windows
    assert controller.agentBrowserAttention[0] is False


# ---------------------------------------------------------------------------
# Shape guards
# ---------------------------------------------------------------------------


def test_agent_browser_lists_match_slot_count(controller):
    """Guard: both lists are indexed `agentSlot - 1`, so they follow the AGENT pool.

    That is the pane registry's high-water mark since 2026-08-15, not
    `_MAX_INSTANCES` — the terminal-agent pool is uncapped, while
    `_MAX_INSTANCES` still bounds the BROWSER registry (below) and the parked
    SDK pool.
    """
    assert len(controller.agentBrowserCount) == controller._agent_pane_slots.rowCount()
    assert len(controller.agentBrowserAttention) == len(controller.agentBrowserCount)


def test_browser_registry_is_still_capped_at_five(controller):
    """The lifted agent cap must not widen the browser's own pool."""
    controller._browser_tabs = {slot: {"url": "", "title": ""} for slot in range(1, 6)}
    assert controller._next_free_browser_slot() is None


def test_shutdown_stops_chrome(controller):
    """The pin rule lives in the running compositor — an IDE that exits
    without releasing its class leaves a rule behind forever."""
    host = chrome(controller)
    controller.shutdown()
    assert host.stop_calls == 1
