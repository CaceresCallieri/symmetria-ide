"""Tests for ChromeHost's spawn / pin / teardown behaviour.

These cover the band where the browser's containment actually lives, and where
unit-testable mistakes are invisible in a happy-path live run: the pin rule
must exist BEFORE Chrome maps a window, every spawn path must carry the
per-IDE `--class`, and a deferred pin must be retried rather than abandoned.

`subprocess.Popen` and `hyprland_ipc` are stubbed — nothing here launches a
browser or touches the compositor (conftest also makes that impossible).
"""

from __future__ import annotations

import pytest

from symmetria_ide import chrome_host, hyprland_ipc


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
    """A ChromeHost with a stubbed Chrome binary and a stubbed compositor."""
    binary = tmp_path / "chrome"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("SYMMETRIA_IDE_CHROME_BIN", str(binary))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SYMMETRIA_IDE_CDP_PORT", "4321")
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "test_sig")

    events: list = []
    spawned: list[FakeProc] = []

    def fake_popen(argv, **_kwargs):
        events.append(("spawn", list(argv)))
        proc = FakeProc(list(argv))
        spawned.append(proc)
        return proc

    monkeypatch.setattr(chrome_host.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        chrome_host.hyprland_ipc,
        "apply_keyword",
        lambda rule: (events.append(("rule", rule)), True)[1],
    )
    monkeypatch.setattr(
        hyprland_ipc.WorkspaceWatcher, "resolve_now", lambda self: (6, "6")
    )
    monkeypatch.setattr(hyprland_ipc.WorkspaceWatcher, "start", lambda self: None)
    monkeypatch.setattr(hyprland_ipc.WorkspaceWatcher, "stop", lambda self: None)

    h = chrome_host.ChromeHost("/home/jc/projects/thing")
    h.test_events = events
    h.test_spawned = spawned
    yield h


def spawn_argv(host) -> list[str]:
    return next(payload for kind, payload in host.test_events if kind == "spawn")


class TestColdStart:
    def test_pin_rule_is_installed_before_chrome_spawns(self, host):
        """A rule only applies at window-map time, so installing it after the
        spawn lets the FIRST window land on the user's workspace — the exact
        interruption the feature exists to prevent."""
        host.open_window("https://a.test", lambda _r: None)
        kinds = [kind for kind, _ in host.test_events]
        assert kinds.index("rule") < kinds.index("spawn")

    def test_cold_start_opens_at_the_requested_url(self, host):
        """Chrome always opens a window at launch. Spawning bare and then
        asking for a window yields a stray chrome://newtab that CDP discovers
        FIRST, so the registry adopts the wrong one."""
        host.open_window("https://a.test", lambda _r: None)
        assert spawn_argv(host)[-1] == "https://a.test"

    def test_spawn_carries_class_profile_and_port(self, host):
        host.open_window("https://a.test", lambda _r: None)
        argv = spawn_argv(host)
        assert f"--class={host.window_class}" in argv
        assert f"--user-data-dir={host.profile}" in argv
        assert "--remote-debugging-port=4321" in argv

    def test_cold_start_does_not_ask_for_a_second_window(self, host):
        """The startup window IS the requested window."""
        host.open_window("https://a.test", lambda _r: None)
        assert len([k for k, _ in host.test_events if k == "spawn"]) == 1

    def test_missing_chrome_is_reported_not_raised(self, host, monkeypatch):
        monkeypatch.setenv("SYMMETRIA_IDE_CHROME_BIN", "/nope/chrome")
        assert host.open_window("https://a.test", lambda _r: None) == (
            "chrome-not-installed"
        )


class TestWarmOpen:
    def test_warm_unattached_open_still_carries_the_class(self, host):
        """This path can WIN the race and become the primary process (session
        restore replays several URLs at once). Without --class its windows fall
        under the default `google-chrome` class, the pin rule misses them, and
        they escape onto whatever workspace the user is looking at."""
        host.open_window("https://a.test", lambda _r: None)  # cold
        host.open_window("https://b.test", lambda _r: None)  # warm, CDP down
        second = [payload for kind, payload in host.test_events if kind == "spawn"][1]
        assert f"--class={host.window_class}" in second
        assert "--new-window" in second
        assert second[-1] == "https://b.test"


class TestPinRetry:
    def test_deferred_pin_is_retried_on_the_next_open(self, host, monkeypatch):
        """When the IDE's own window isn't mapped yet the pin is skipped. If it
        were never retried, EVERY window of that session would escape."""
        monkeypatch.setattr(
            hyprland_ipc.WorkspaceWatcher, "resolve_now", lambda self: (0, "")
        )
        host.open_window("https://a.test", lambda _r: None)
        assert not [k for k, _ in host.test_events if k == "rule"]

        monkeypatch.setattr(
            hyprland_ipc.WorkspaceWatcher, "resolve_now", lambda self: (6, "6")
        )
        host.open_window("https://b.test", lambda _r: None)
        assert [k for k, _ in host.test_events if k == "rule"]

    def test_a_successful_pin_is_not_reapplied_every_open(self, host):
        host.open_window("https://a.test", lambda _r: None)
        host.open_window("https://b.test", lambda _r: None)
        assert len([k for k, _ in host.test_events if k == "rule"]) == 1


class TestTeardown:
    def test_stop_releases_the_rule_and_kills_chrome(self, host):
        """Window rules live in the RUNNING compositor — an IDE that exits
        without releasing its class leaves one pointing at a workspace
        forever."""
        host.open_window("https://a.test", lambda _r: None)
        host.stop()
        rules = [payload for kind, payload in host.test_events if kind == "rule"]
        assert "workspace unset" in rules[-1]
        assert host.test_spawned[0].terminated

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
