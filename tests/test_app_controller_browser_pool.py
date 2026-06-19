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
# jump_to_focused_agent_browser — Ctrl+Shift+B, the keyboard twin of the chip
# globe now that the standalone browser tab is gone (agent-only reachability).
# ---------------------------------------------------------------------------


def test_jump_to_agent_browser_jumps_when_focused_agent_owns_window(controller):
    """With the focused agent owning a window, the chord jumps to it (and
    switches the surface, via focus_agent_browser → focus_browser)."""
    aid = f"{os.getpid()}_1"  # agent slot 1
    controller._open_browser_for_mcp("https://x.com", aid)  # agent 1 owns window 1
    controller._focused_term_agent = 1
    controller.set_central_surface("editor")

    controller.jump_to_focused_agent_browser()

    assert controller.centralSurface == "browser"
    assert controller.focusedBrowser == 1


def test_jump_to_agent_browser_noop_when_focused_agent_owns_nothing(controller):
    """Agent-only reachability: with no owned window there is nowhere to jump,
    so the chord is a silent no-op (no surface change)."""
    controller._focused_term_agent = 1  # focused but owns no window
    controller.set_central_surface("editor")
    emissions = _capture(controller.centralSurfaceChanged)

    controller.jump_to_focused_agent_browser()

    assert controller.centralSurface == "editor"
    assert emissions == []


def test_jump_to_agent_browser_noop_when_no_agent_focused(controller):
    """No focused agent (empty pool) → no-op, same agent-only rationale."""
    assert controller._focused_term_agent == 0
    controller.set_central_surface("editor")
    controller.jump_to_focused_agent_browser()
    assert controller.centralSurface == "editor"


def test_jump_to_agent_browser_returns_to_terminal_when_on_browser(controller):
    """Already on the browser surface → home to the terminal (the keyboard way
    back out; same asymmetry as the other surface chords).

    The focused agent ALSO owns a window here, so this pins the branch ORDER:
    the early-return on "already on browser" must win over the "owns a window →
    jump" branch (a reordering of the checks would otherwise re-jump and never
    leave the surface)."""
    aid = f"{os.getpid()}_1"
    controller._open_browser_for_mcp("https://x.com", aid)  # agent 1 owns a window
    controller._focused_term_agent = 1
    controller.set_central_surface("browser")
    controller.jump_to_focused_agent_browser()
    assert controller.centralSurface == "terminal"


# ---------------------------------------------------------------------------
# Per-project agent-browser gate (the committable .symmetria/ide.json marker).
# ---------------------------------------------------------------------------


def test_project_browser_default_off(controller, tmp_path):
    """A project with no marker reads as disabled after anchoring to it."""
    (tmp_path / ".git").mkdir()  # make tmp_path a deterministic project root
    controller.anchor_to_path(str(tmp_path))
    assert controller.projectBrowserEnabled is False


def test_toggle_project_browser_flips_and_persists(controller, tmp_path):
    """The MCP-popup toggle flips the flag, emits its notify, and writes the
    committable marker; toggling again disables it."""
    from symmetria_ide import project_browser_marker as pbm

    # Plant a `.git` so resolve_project_root anchors the marker AT tmp_path —
    # never walking up into the real repo (which would write a committable
    # marker into this project's tree). Bulletproofs isolation regardless of
    # where pytest roots its tmp dir.
    (tmp_path / ".git").mkdir()
    controller.anchor_to_path(str(tmp_path))
    emissions = _capture(controller.projectBrowserEnabledChanged)

    controller.toggle_project_browser()
    assert controller.projectBrowserEnabled is True
    assert len(emissions) == 1
    assert pbm.browser_agents_enabled(str(tmp_path)) is True
    assert (tmp_path / ".symmetria" / "ide.json").exists()

    controller.toggle_project_browser()
    assert controller.projectBrowserEnabled is False
    assert len(emissions) == 2
    assert pbm.browser_agents_enabled(str(tmp_path)) is False


def test_spawn_argv_gates_browser_mcp(controller, tmp_path, monkeypatch):
    """agent_spawn_argv injects --mcp-config ONLY when the project opted in.

    With a fake server port + a spawned slot, the per-agent config (hence the
    --mcp-config flag claude loads it through) appears only after the project
    is enabled — the harness-agnostic gate in action. Config writes are routed
    to tmp via gettempdir so the test leaves no temp-dir litter."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    (tmp_path / ".git").mkdir()  # anchor the marker at tmp_path, never the repo
    controller.anchor_to_path(str(tmp_path))
    # Pretend the browser MCP server bound a port so agent_config_path can
    # write a config when the gate allows it.
    controller._browser_mcp_server._port = 54321
    controller.spawn_agent("fresh", True, "claude")
    slot = controller.agentOrder[0]

    # OFF (default): the gate returns "" → no --mcp-config flag.
    assert "--mcp-config" not in controller.agent_spawn_argv(slot)

    # ON: enable; the spawn-time re-read picks it up → --mcp-config present.
    controller.toggle_project_browser()
    assert "--mcp-config" in controller.agent_spawn_argv(slot)


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
    # Returns the display number plus url/title — the correlator an agent hands
    # to chrome-devtools-mcp's select_page (the committed url settles later).
    assert controller._open_browser_for_mcp("https://x.com") == {
        "ok": True,
        "window": 1,
        "url": "https://x.com",
        "title": "",
    }
    assert controller.browserUrls[0] == "https://x.com"
    # Second open appends as display position 2.
    assert controller._open_browser_for_mcp("about:blank") == {
        "ok": True,
        "window": 2,
        "url": "about:blank",
        "title": "",
    }


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
#
# NB: these call _record_browser_attribution DIRECTLY. Since the Stage-4
# chrome-devtools-mcp migration nothing in production calls it (page driving
# bypasses our bridge), so the PULSE path exercised here is a deliberately
# DORMANT hook — kept (and tested) for the future CDP monitor that re-activates
# it. The OWNERSHIP path (glyph visibility, click-jump) IS live via
# _open_browser_for_mcp → _claim_browser_window.
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
    assert result == {"ok": True, "window": 1, "url": "https://x.com", "title": ""}
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


def test_close_agent_desync_branch_also_prunes_browser_links(controller):
    """close_agent's defensive desync branch (slot in _term_agents but not
    _agent_order) must prune browser links too — it calls the same helper."""
    aid = f"{os.getpid()}_2"
    controller.open_browser()  # browser slot 1
    controller._record_browser_attribution(aid, 1, "start")
    assert controller.agentBrowserCount[1] == 1
    # Force the desync: agent present in _term_agents but absent from _agent_order.
    controller._term_agents[2] = {"harness": "claude", "title": ""}
    # _agent_order intentionally left without slot 2 → desync recovery path.

    controller.close_agent(2)  # must not raise, and must prune
    assert controller.agentBrowserCount[1] == 0


def test_attribution_end_after_agent_close_does_not_resurrect_or_emit(controller):
    """A late op 'end' arriving after the agent was closed (its counter
    dropped) must not resurrect ownership nor fire a spurious change signal."""
    aid = f"{os.getpid()}_2"
    controller.open_browser()  # browser slot 1
    controller._record_browser_attribution(aid, 1, "start")  # owns + pulsing
    controller._drop_agent_browser_links(2)  # simulate the agent closing mid-op
    assert controller.agentBrowserCount[1] == 0
    assert controller.agentBrowserActive[1] is False

    emissions = _capture(controller.agentBrowserChanged)
    controller._record_browser_attribution(aid, 1, "end")  # the late op result
    assert controller.agentBrowserCount[1] == 0  # no ownership resurrection
    assert controller.agentBrowserActive[1] is False
    assert emissions == []  # guarded emit — no spurious re-bind


# ---------------------------------------------------------------------------
# Agent-owned browser (notify, don't yank) — an agent opening a window lights
# the chip globe but must NOT pull the user's surface; they jump on their own
# terms. The user previously got yanked to the browser on every agent open.
# ---------------------------------------------------------------------------


def test_open_browser_for_mcp_does_not_switch_surface(controller):
    aid = f"{os.getpid()}_1"
    controller.set_central_surface("editor")
    surf_emissions = _capture(controller.centralSurfaceChanged)

    result = controller._open_browser_for_mcp("https://x.com", aid)

    assert result["ok"] is True
    assert controller.centralSurface == "editor"  # NOT yanked to browser
    assert controller.focusedBrowser == 0  # focus untouched (no focus_browser)
    assert surf_emissions == []
    # The window still exists + is owned → the globe lights; just not focused.
    assert list(controller.browserOrder) == [1]
    assert controller.agentBrowserCount[0] == 1


def test_manual_open_browser_still_focuses_and_switches(controller):
    """The manual path (Ctrl+T → open_browser, focus default True) is unchanged
    — only the agent path opts out of the surface switch."""
    controller.set_central_surface("editor")
    controller.open_browser("https://x.com")  # focus defaults True
    assert controller.centralSurface == "browser"
    assert controller.focusedBrowser == 1


# ---------------------------------------------------------------------------
# Attention badge — browser_request_attention lights the dot on the agent's
# globe; cleared on view (focus_agent_browser), window close, or agent death.
# ---------------------------------------------------------------------------


def test_request_attention_lights_dot_for_owning_agent(controller):
    aid = f"{os.getpid()}_2"  # agent slot 2 → index 1
    controller._open_browser_for_mcp("https://x.com", aid)  # agent owns a window
    emissions = _capture(controller.agentBrowserChanged)

    result = controller._set_browser_attention_for_mcp(aid, "look here")

    assert result == {"ok": True}
    assert controller.agentBrowserAttention[1] is True
    assert len(emissions) == 1
    assert controller._agent_browser_attention[2] == "look here"  # message stored


def test_request_attention_rejects_agent_without_window(controller):
    """The dot rides the globe (only shown when the agent owns ≥1 window), so a
    request with no window is rejected rather than stored-but-invisible."""
    aid = f"{os.getpid()}_2"
    result = controller._set_browser_attention_for_mcp(aid)
    assert result == {"ok": False, "error": "no-window"}
    assert controller.agentBrowserAttention[1] is False


def test_request_attention_rejects_foreign_or_untagged(controller):
    controller._open_browser_for_mcp("https://x.com", f"{os.getpid()}_2")
    assert controller._set_browser_attention_for_mcp("") == {
        "ok": False,
        "error": "unknown-agent",
    }
    assert controller._set_browser_attention_for_mcp(f"{os.getpid() + 1}_2") == {
        "ok": False,
        "error": "unknown-agent",
    }
    assert controller.agentBrowserAttention == [False] * controller.maxAgentSlots


def test_focus_agent_browser_clears_attention(controller):
    aid = f"{os.getpid()}_1"
    controller._open_browser_for_mcp("https://x.com", aid)
    controller._set_browser_attention_for_mcp(aid, "look")
    assert controller.agentBrowserAttention[0] is True

    controller.focus_agent_browser(1)  # viewing it is what clears the dot
    assert controller.agentBrowserAttention[0] is False


def test_closing_last_window_clears_attention(controller):
    """When an agent's last window closes, its attention is dropped so a fresh
    window later doesn't re-light a stale dot."""
    aid = f"{os.getpid()}_1"
    controller._open_browser_for_mcp("https://x.com", aid)  # window slot 1
    controller._set_browser_attention_for_mcp(aid, "look")
    assert controller.agentBrowserAttention[0] is True

    controller.close_browser(1)
    assert controller.agentBrowserAttention[0] is False


def test_attention_persists_until_agents_last_window_closes(controller):
    """With an agent owning TWO windows, closing the first KEEPS attention (the
    `not owned` guard in _drop_browser_window_links only clears on the agent's
    LAST window); closing the second finally clears it."""
    aid = f"{os.getpid()}_1"
    controller._open_browser_for_mcp("https://a.com", aid)  # window slot 1
    controller._open_browser_for_mcp("https://b.com", aid)  # window slot 2
    controller._set_browser_attention_for_mcp(aid, "look")
    assert controller.agentBrowserCount[0] == 2
    assert controller.agentBrowserAttention[0] is True

    controller.close_browser(1)  # still owns window 2 → attention stays
    assert controller.agentBrowserAttention[0] is True

    controller.close_browser(2)  # last window gone → attention cleared
    assert controller.agentBrowserAttention[0] is False


# ---------------------------------------------------------------------------
# Leak safety — closing an agent must close the windows it SOLELY owns (free
# the WebEngineView + avoid an unreachable orphan now the tab is gone), while
# a window co-owned by another living agent is kept.
# ---------------------------------------------------------------------------


def test_close_agent_closes_its_solely_owned_window(controller):
    aid = f"{os.getpid()}_2"
    controller._open_browser_for_mcp("https://x.com", aid)  # agent 2 owns window 1
    assert list(controller.browserOrder) == [1]
    controller._term_agents[2] = {"harness": "claude", "title": ""}
    controller._agent_order = [2]

    controller.close_agent(2)

    # The orphaned window is closed (RAM freed), not left dangling.
    assert list(controller.browserOrder) == []
    assert controller.browserSlotActive == [False] * controller.maxBrowserSlots
    assert controller.agentBrowserCount[1] == 0


def test_close_agent_keeps_window_co_owned_by_a_living_agent(controller):
    """Two agents drive the same window; closing one keeps it for the other,
    then closing the second finally frees it."""
    aid_a = f"{os.getpid()}_2"  # index 1
    aid_b = f"{os.getpid()}_3"  # index 2
    controller._open_browser_for_mcp("https://x.com", aid_a)  # window 1, owned by A
    controller._record_browser_attribution(aid_b, 1, "start")  # B co-drives window 1
    assert controller.agentBrowserCount[1] == 1
    assert controller.agentBrowserCount[2] == 1

    # Close agent A — window 1 is still owned by living agent B, so it stays.
    controller._term_agents[2] = {"harness": "claude", "title": ""}
    controller._agent_order = [2]
    controller.close_agent(2)
    assert list(controller.browserOrder) == [1]  # window kept
    assert controller.agentBrowserCount[1] == 0  # A's link gone
    assert controller.agentBrowserCount[2] == 1  # B still owns it

    # Close agent B — now solely owned → finally freed.
    controller._term_agents[3] = {"harness": "claude", "title": ""}
    controller._agent_order = [3]
    controller.close_agent(3)
    assert list(controller.browserOrder) == []
    assert controller.agentBrowserCount[2] == 0
