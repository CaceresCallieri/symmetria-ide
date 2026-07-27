"""Tests for ChromeHost's spawn / containment / teardown behaviour.

These cover the band where the browser's containment actually lives, and where
a mistake is invisible in a happy-path live run. Under the nested-compositor
backend containment is entirely a property of the ENVIRONMENT Chrome is spawned
with: point it at our Wayland socket and its windows are ours, leave any door
open to the host display and a loose browser window lands on whatever workspace
the user is looking at. Every spawn path must therefore carry the full env, and
the spawn must refuse to happen at all when the socket is missing.

`subprocess.Popen` is stubbed — nothing here launches a browser (conftest also
makes that impossible).
"""

from __future__ import annotations

import os

import pytest

from symmetria_ide import chrome_host


class FakeProc:
    def __init__(self, argv):
        self.argv = argv
        self.terminated = False
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def wait(self):
        return 0

    def terminate(self):
        self.terminated = True
        self._alive = False


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A ChromeHost with a stubbed Chrome binary and a live compositor socket."""
    binary = tmp_path / "chrome"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("SYMMETRIA_IDE_CHROME_BIN", str(binary))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SYMMETRIA_IDE_CDP_PORT", "4321")

    # Stand in for the nested compositor's socket. ChromeHost only checks that
    # it exists — the compositor itself is QML, created with the engine.
    runtime = tmp_path / "run"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("DISPLAY", ":0")  # the X11 door that must be shut
    monkeypatch.setenv("WAYLAND_SOCKET", "9")  # the fd-passing door, likewise
    (runtime / chrome_host.browser_identity(os.getpid())).write_text("")

    spawns: list[dict] = []

    def fake_popen(argv, **kwargs):
        proc = FakeProc(list(argv))
        # The proc is recorded alongside the argv so teardown tests can reach
        # it without re-patching Popen and shadowing this fake.
        spawns.append(
            {"argv": list(argv), "env": kwargs.get("env") or {}, "proc": proc}
        )
        return proc

    monkeypatch.setattr(chrome_host.subprocess, "Popen", fake_popen)

    h = chrome_host.ChromeHost("/home/jc/projects/thing")
    h.test_spawns = spawns
    h.test_runtime = runtime
    yield h


class TestColdStart:
    def test_cold_start_opens_at_the_requested_url(self, host):
        """Chrome always opens a window at launch. Spawning bare and then
        asking for a window yields a stray chrome://newtab that CDP discovers
        FIRST, so the registry adopts the wrong one."""
        host.open_window("https://a.test", lambda _r: None)
        assert host.test_spawns[0]["argv"][-1] == "https://a.test"

    def test_spawn_carries_profile_port_and_ozone(self, host):
        host.open_window("https://a.test", lambda _r: None)
        argv = host.test_spawns[0]["argv"]
        assert f"--user-data-dir={host.profile}" in argv
        assert "--remote-debugging-port=4321" in argv
        # Without this Chrome may choose XWayland, which talks to the HOST
        # display — a loose window on the user's workspace.
        assert "--ozone-platform=wayland" in argv

    def test_spawn_suppresses_the_crash_restore_bubble(self, host):
        """We terminate Chrome on IDE quit and it scores that as an unclean
        exit, so without this EVERY launch opens with a "Restore pages?"
        popup over the page — in a pane with no room for it, and offering to
        restore a session nobody wants back."""
        host.open_window("https://a.test", lambda _r: None)
        assert "--hide-crash-restore-bubble" in host.test_spawns[0]["argv"]

    def test_spawn_points_chrome_at_our_compositor(self, host):
        host.open_window("https://a.test", lambda _r: None)
        env = host.test_spawns[0]["env"]
        assert env["WAYLAND_DISPLAY"] == host.wayland_socket

    def test_spawn_removes_the_x11_fallback(self, host):
        """DISPLAY must be REMOVED, not overridden: Chrome falls back to
        XWayland when Wayland is unavailable, and an X11 Chrome escapes onto
        the host. With no DISPLAY a broken socket fails loudly instead."""
        host.open_window("https://a.test", lambda _r: None)
        assert "DISPLAY" not in host.test_spawns[0]["env"]
        # libwayland PREFERS the fd-passing form over WAYLAND_DISPLAY, so
        # leaving it set would route Chrome to the host compositor.
        assert "WAYLAND_SOCKET" not in host.test_spawns[0]["env"]

    def test_cold_start_does_not_ask_for_a_second_window(self, host):
        """The startup window IS the requested window."""
        host.open_window("https://a.test", lambda _r: None)
        assert len(host.test_spawns) == 1

    def test_missing_chrome_is_reported_not_raised(self, host, monkeypatch):
        monkeypatch.setenv("SYMMETRIA_IDE_CHROME_BIN", "/nope/chrome")
        assert host.open_window("https://a.test", lambda _r: None) == (
            "chrome-not-installed"
        )


class TestCompositorGate:
    def test_no_socket_means_no_spawn(self, host):
        """Spawning Chrome without the nested socket is worse than failing:
        with nowhere of ours to connect to, it opens on the user's desktop."""
        (host.test_runtime / chrome_host.browser_identity(os.getpid())).unlink()
        assert host.open_window("https://a.test", lambda _r: None) == (
            "compositor-not-ready"
        )
        assert host.test_spawns == []


class TestWarmOpen:
    def test_warm_unattached_open_carries_the_full_environment(self, host):
        """This path can WIN the race and become the primary process (session
        restore replays several URLs at once). A primary process spawned
        without our WAYLAND_DISPLAY would connect to the HOST compositor — a
        loose browser window on the user's workspace, only under a race, so it
        will not show up in casual testing."""
        host.open_window("https://a.test", lambda _r: None)  # cold
        host.open_window("https://b.test", lambda _r: None)  # warm, CDP down
        second = host.test_spawns[1]
        assert second["env"]["WAYLAND_DISPLAY"] == host.wayland_socket
        assert "DISPLAY" not in second["env"]
        # The other half of containment: without this Chrome may pick
        # XWayland, which talks to the HOST display.
        assert "--ozone-platform=wayland" in second["argv"]
        assert "--new-window" in second["argv"]
        assert second["argv"][-1] == "https://b.test"


class TestIdentity:
    def test_socket_and_app_id_are_the_same_string(self, host):
        """One identity in two protocols: the pane matches OUR toplevels by
        app_id, and Chrome reaches us over the socket of that name."""
        host.open_window("https://a.test", lambda _r: None)
        assert host.wayland_socket == host.window_class
        assert f"--class={host.window_class}" in host.test_spawns[0]["argv"]


class TestTeardown:
    def test_stop_kills_chrome(self, host):
        host.open_window("https://a.test", lambda _r: None)
        host.stop()
        assert host.test_spawns[0]["proc"].terminated

    def test_chrome_dying_is_announced_once_attached(self, host):
        """The registry must not outlive the browser, or agents drive nothing."""
        host.open_window("https://a.test", lambda _r: None)
        seen: list[int] = []
        host.browserGone.connect(lambda: seen.append(1))
        host._on_cdp_disconnected()
        assert seen == [1]

    def test_our_own_teardown_does_not_announce_a_death(self, host):
        """stop() closes the socket itself; treating that as Chrome dying
        would fire a registry wipe during shutdown."""
        host.open_window("https://a.test", lambda _r: None)
        seen: list[int] = []
        host.browserGone.connect(lambda: seen.append(1))
        host.stop()
        host._on_cdp_disconnected()
        assert seen == []
