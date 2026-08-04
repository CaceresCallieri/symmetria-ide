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

import io
import os
from types import SimpleNamespace

import pytest

from symmetria_ide import chrome_host


class FakeProc:
    def __init__(self, argv, stderr_lines=(), exit_status=0):
        self.argv = argv
        self.terminated = False
        self.waited = False
        self._alive = True
        # Negative is `-signum`, as Popen reports it. Chrome's FATAL raises
        # SIGTRAP, so -5 is what a real crash looks like here.
        self._exit_status = exit_status
        # Bytes, like the real pipe yields.
        self.stderr = iter(list(stderr_lines))

    def poll(self):
        return None if self._alive else 0

    def wait(self):
        self.waited = True
        return self._exit_status

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
        # it without re-patching Popen and shadowing this fake. `stderr` is
        # recorded too — asserting on the FAKE's own attribute would pass for
        # every possible production value, including the DEVNULL this
        # deliberately moved away from.
        spawns.append(
            {
                "argv": list(argv),
                "env": kwargs.get("env") or {},
                "stderr": kwargs.get("stderr"),
                "proc": proc,
            }
        )
        return proc

    monkeypatch.setattr(chrome_host.subprocess, "Popen", fake_popen)

    # Real drain threads would run concurrently with caplog's handler teardown
    # ("I/O operation on closed file"), and this suite already has a
    # load-sensitive intermittency problem worth not feeding — see
    # .claude/memory/reference/qt-pyside/processevents_shared_app_segv.md. The
    # drain itself is still exercised, synchronously, by the tests that call
    # `_drain_chrome_stderr` directly.
    monkeypatch.setattr(
        chrome_host.threading,
        "Thread",
        lambda *a, **k: SimpleNamespace(start=lambda: None),
    )

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


class TestCrashForensics:
    """Chrome dies by printing a reason to stderr and raising SIGTRAP. The core
    it leaves is a stripped binary, so that line is the ONLY readable account —
    discard it and a crash reads as "the browser silently vanished"."""

    def test_stderr_is_piped_not_discarded(self, host):
        """Asserted on the kwarg the spawn actually passed. Checking the fake's
        own `.stderr` attribute instead would hold for every possible
        production value — including the DEVNULL this moved away from."""
        host.open_window("https://a.test", lambda _r: None)
        assert host.test_spawns[0]["stderr"] is chrome_host.subprocess.PIPE

    def test_warm_open_also_pipes_stderr(self, host):
        """The path most likely to die unexplained: its own docstring says it
        can lose the handoff race and BECOME the primary Chrome."""
        host.open_window("https://a.test", lambda _r: None)  # cold
        host.open_window("https://b.test", lambda _r: None)  # warm, CDP down
        assert host.test_spawns[1]["stderr"] is chrome_host.subprocess.PIPE

    def test_fatal_lines_are_logged_at_error(self, host, caplog):
        proc = FakeProc(
            [],
            [
                b"[123:456] some routine warning\n",
                b"[123:456] Wayland protocol error: wl_pointer@17\n",
            ],
        )
        with caplog.at_level("ERROR"):
            host._drain_chrome_stderr(proc)
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        # Both halves. Chrome warns about fonts, GPU and dbus on every launch,
        # so a regression that promotes EVERY line to error would drown the log
        # in noise while still passing a one-sided assertion.
        assert len(errors) == 1
        assert "protocol error" in errors[0].getMessage()
        assert "routine warning" not in errors[0].getMessage()

    def test_draining_also_reaps(self, host):
        """The drain replaces the reaper thread's wait(), so it has to do that
        job too — and an unread PIPE would block Chrome once it fills."""
        proc = FakeProc([], [b"noise\n"])
        host._drain_chrome_stderr(proc)
        assert proc.waited

    def test_the_drain_is_handed_its_process(self, host):
        """It must not re-read `self._proc`: `stop()` clears that and a respawn
        rebinds it, so a thread reading it later would either skip its own
        process — leaking the pipe and leaving a zombie — or start draining a
        different one alongside its real reader."""
        proc = FakeProc([], [b"noise\n"])
        host._proc = None  # as stop() leaves it
        host._drain_chrome_stderr(proc)
        assert proc.waited


# The real GPU-death sequence, in order, from a `strings` read of the installed
# Chrome. Only the LAST line matches the drain's loud-line allow-list, which is
# how three crashes came to be recorded as a lone context-free `Goodbye.`.
GPU_DEATH = [
    b"[123:456] The GPU process has crashed 3 time(s)\n",
    b"[123:456] GPU process exited unexpectedly: exit_code=15\n",
    (
        b"[123:456] FATAL:gpu_data_manager_impl_private.cc:416] "
        b"GPU process isn't usable. Goodbye.\n"
    ),
]


class TestStderrHistory:
    """The allow-list alone kept the LAST line of a GPU failure and dropped the
    three before it that say why — see `_drain_chrome_stderr`."""

    def test_a_bad_exit_dumps_what_chrome_said_first(self, host, caplog):
        proc = FakeProc([], GPU_DEATH, exit_status=-5)
        with caplog.at_level("DEBUG"):
            host._drain_chrome_stderr(proc)
        dump = "\n".join(
            r.getMessage() for r in caplog.records if r.levelname == "WARNING"
        )
        # The two lines the allow-list had no token for. Asserting on the FATAL
        # would pass against the old code, which already logged that one.
        assert "crashed 3 time(s)" in dump
        assert "exit_code=15" in dump

    def test_a_bad_exit_names_the_signal(self, host, caplog):
        """-5 is SIGTRAP, which Chrome raises on its OWN failed CHECK. Reading
        it as "something killed us" points the next investigation outward, at
        the OOM killer and the session manager, where there is nothing."""
        proc = FakeProc([], GPU_DEATH, exit_status=-5)
        with caplog.at_level("DEBUG"):
            host._drain_chrome_stderr(proc)
        assert any(
            "signal 5" in r.getMessage()
            for r in caplog.records
            if r.levelname == "WARNING"
        )

    def test_our_own_teardown_dumps_nothing(self, host, caplog):
        """`stop()` SIGTERMs Chrome on every IDE quit. A tail dumped there
        teaches the reader to skip the block, which is only ever read once."""
        host._stopping = True
        proc = FakeProc([], GPU_DEATH, exit_status=-15)
        with caplog.at_level("DEBUG"):
            host._drain_chrome_stderr(proc)
        assert not [r for r in caplog.records if r.levelname == "WARNING"]

    def test_a_clean_exit_dumps_nothing(self, host, caplog):
        """The warm `--new-window` handoff ends this way every time it wins."""
        proc = FakeProc([], [b"[123:456] some routine warning\n"], exit_status=0)
        with caplog.at_level("DEBUG"):
            host._drain_chrome_stderr(proc)
        assert not [r for r in caplog.records if r.levelname == "WARNING"]

    def test_the_history_is_bounded(self, host, caplog):
        """A browser left open for days must not accumulate its whole stderr in
        memory waiting for a crash that may never come."""
        flood = [
            b"chatter %d\n" % n for n in range(chrome_host._STDERR_HISTORY_LINES * 3)
        ]
        proc = FakeProc([], flood, exit_status=-5)
        with caplog.at_level("DEBUG"):
            host._drain_chrome_stderr(proc)
        dump = "\n".join(
            r.getMessage() for r in caplog.records if r.levelname == "WARNING"
        )
        assert "chatter 0" not in dump  # evicted
        assert f"chatter {len(flood) - 1}" in dump  # the newest survives


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


class TestMachineStateDiagnostic:
    """`_machine_state` runs in the log line for an UNEXPECTED Chrome death.

    That is its whole hazard: it executes only when something has already gone
    wrong, so a regression here is discovered exactly when the diagnostic was
    needed and no longer available. Worse, it sits upstream of
    `browserGone.emit()` — a raise would swallow the signal that drops the
    window registry, turning a browser crash into a browser crash the IDE does
    not notice.
    """

    def test_it_reports_available_memory_not_free(self, monkeypatch):
        """MemFree is near zero on a healthy machine because the page cache
        holds it — reading the wrong one is how "out of memory" gets diagnosed
        where there is none."""
        monkeypatch.setattr(
            chrome_host,
            "open",
            lambda *a, **k: io.StringIO(
                "MemFree: 12345 kB\nMemAvailable: 4194304 kB\n"
            ),
            raising=False,
        )
        assert "4.0GiB available" in chrome_host._machine_state()

    @pytest.mark.parametrize(
        "meminfo",
        [
            pytest.param("MemTotal: 1 kB\n", id="no-MemAvailable-line"),
            pytest.param("MemAvailable: not-a-number kB\n", id="unparseable-value"),
            pytest.param("MemAvailable:\n", id="value-field-missing"),
        ],
    )
    def test_a_malformed_meminfo_degrades_instead_of_raising(
        self, monkeypatch, meminfo
    ):
        """The last two raise ValueError and IndexError respectively — neither
        is an OSError, so a narrower except would let them out."""
        monkeypatch.setattr(
            chrome_host, "open", lambda *a, **k: io.StringIO(meminfo), raising=False
        )
        assert "mem unknown" in chrome_host._machine_state()

    def test_an_unreadable_procfs_degrades(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise OSError("procfs gone")

        monkeypatch.setattr(chrome_host, "open", boom, raising=False)
        assert "mem unknown" in chrome_host._machine_state()

    def test_load_is_reported_and_its_failure_is_survivable(self, monkeypatch):
        assert "load " in chrome_host._machine_state()

        def boom():
            raise OSError("no loadavg")

        monkeypatch.setattr(chrome_host.os, "getloadavg", boom)
        state = chrome_host._machine_state()
        assert "load unknown" in state
        # The other half must still be reported — the two are independent.
        assert "available" in state
