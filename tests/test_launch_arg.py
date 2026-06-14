"""Tests for the `symmetria-ide [PATH]` launch-argument resolver.

`_apply_project_arg` chdir's into an optional project directory before
QGuiApplication / AppController spin up, so the whole stack (editor nvim,
shell, file tree, git pane) opens on the right project via the process cwd.
It must also pass Qt flags through untouched and never abort on a bad path.
"""

from __future__ import annotations

import os
import socket
import tempfile
import time

import pytest

from symmetria_ide.app import (
    _apply_project_arg,
    _reap_orphan_nvim_sockets,
    _resolve_launch_dir,
    _socket_is_live,
)


@pytest.fixture(autouse=True)
def _restore_cwd():
    """`_apply_project_arg` chdir's as a side effect — restore after each test."""
    saved = os.getcwd()
    yield
    os.chdir(saved)


def test_valid_path_chdirs_and_is_stripped_from_argv(tmp_path):
    qt_argv = _apply_project_arg(["symmetria-ide", str(tmp_path)])
    # cwd moved into the project (resolve both sides — macOS/symlinked /tmp).
    assert os.path.realpath(os.getcwd()) == os.path.realpath(str(tmp_path))
    # The path is consumed; only the program name remains for Qt.
    assert qt_argv == ["symmetria-ide"]


def test_no_arg_leaves_cwd_unchanged():
    before = os.getcwd()
    qt_argv = _apply_project_arg(["symmetria-ide"])
    assert os.getcwd() == before
    assert qt_argv == ["symmetria-ide"]


def test_bad_path_falls_back_to_cwd_without_raising():
    before = os.getcwd()
    # Must NOT raise — launch is resilient to a typo'd path.
    qt_argv = _apply_project_arg(["symmetria-ide", "/no/such/dir/xyz123"])
    assert os.getcwd() == before
    assert qt_argv == ["symmetria-ide"]


def test_file_path_is_rejected_as_non_directory(tmp_path):
    a_file = tmp_path / "afile.txt"
    a_file.write_text("x")
    before = os.getcwd()
    _apply_project_arg(["symmetria-ide", str(a_file)])
    # A file is not a project directory — fall back, don't chdir.
    assert os.getcwd() == before


def test_qt_flags_pass_through(tmp_path):
    qt_argv = _apply_project_arg(
        ["symmetria-ide", str(tmp_path), "-platform", "offscreen"]
    )
    # Project path consumed; Qt flags preserved for QGuiApplication.
    assert qt_argv == ["symmetria-ide", "-platform", "offscreen"]


def test_tilde_is_expanded(tmp_path, monkeypatch):
    # ~ resolves against $HOME; point HOME at a real dir and pass "~".
    monkeypatch.setenv("HOME", str(tmp_path))
    _apply_project_arg(["symmetria-ide", "~"])
    assert os.path.realpath(os.getcwd()) == os.path.realpath(str(tmp_path))


def test_no_arg_from_home_redirects_to_scratch(tmp_path, monkeypatch):
    """The thermal-bug guard: launching with no arg from $HOME must NOT keep
    $HOME as the cwd (fff.nvim would recursively watch the whole home tree).
    `_apply_project_arg` chdir's into an inert scratch dir and creates it."""
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    os.chdir(home)  # launch cwd == $HOME

    _apply_project_arg(["symmetria-ide"])

    expected = state / "symmetria-ide" / "scratch"
    assert os.path.realpath(os.getcwd()) == os.path.realpath(str(expected))
    assert expected.is_dir()


class TestResolveLaunchDir:
    """Pure decision helper behind `_apply_project_arg` — no chdir side effect."""

    def test_valid_path_arg_returns_abspath(self, tmp_path):
        assert _resolve_launch_dir(
            cwd="/anywhere", home="/home/u", path_arg=str(tmp_path)
        ) == os.path.abspath(str(tmp_path))

    def test_invalid_path_arg_returns_none(self):
        assert (
            _resolve_launch_dir(
                cwd="/anywhere", home="/home/u", path_arg="/no/such/dir/xyz123"
            )
            is None
        )

    def test_home_cwd_no_arg_returns_scratch(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        monkeypatch.setenv("XDG_STATE_HOME", str(state))
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        result = _resolve_launch_dir(cwd=home, home=home, path_arg=None)
        assert result == str(state / "symmetria-ide" / "scratch")

    def test_filesystem_root_cwd_no_arg_returns_scratch(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        monkeypatch.setenv("XDG_STATE_HOME", str(state))
        result = _resolve_launch_dir(cwd="/", home=str(tmp_path), path_arg=None)
        assert result == str(state / "symmetria-ide" / "scratch")

    def test_normal_project_cwd_no_arg_returns_none(self, tmp_path):
        proj = str(tmp_path / "proj")
        os.makedirs(proj, exist_ok=True)
        assert (
            _resolve_launch_dir(cwd=proj, home=str(tmp_path / "home"), path_arg=None)
            is None
        )

    def test_scratch_falls_back_to_local_state_when_xdg_unset(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        result = _resolve_launch_dir(cwd=home, home=home, path_arg=None)
        assert result == os.path.join(
            home, ".local", "state", "symmetria-ide", "scratch"
        )


class TestSocketIsLive:
    """`_socket_is_live` probes whether an AF_UNIX socket has a live listener."""

    def test_live_listener_is_detected(self):
        base = tempfile.mkdtemp(prefix="reap-")
        sock_path = os.path.join(base, "nvim.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(sock_path)
            srv.listen(1)
            assert _socket_is_live(sock_path) is True
        finally:
            srv.close()
            _rmtree(base)

    def test_missing_socket_file_is_not_live(self):
        base = tempfile.mkdtemp(prefix="reap-")
        try:
            assert _socket_is_live(os.path.join(base, "nvim.sock")) is False
        finally:
            _rmtree(base)

    def test_stale_socket_file_without_listener_is_not_live(self):
        # A bound-then-closed socket leaves the FILE behind, but connect()
        # gets ECONNREFUSED — exactly the dead-owner case the reaper targets.
        base = tempfile.mkdtemp(prefix="reap-")
        sock_path = os.path.join(base, "nvim.sock")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(sock_path)
        s.close()  # leaves sock_path on disk, no listener
        try:
            assert _socket_is_live(sock_path) is False
        finally:
            _rmtree(base)


class TestReapOrphanNvimSockets:
    """Startup sweep of leaked `symmetria-nvim-*` socket dirs."""

    def test_reaps_dead_spares_live_and_fresh(self, monkeypatch):
        base = tempfile.mkdtemp(prefix="reap-base-")
        monkeypatch.setattr(tempfile, "gettempdir", lambda: base)
        old = time.time() - 120.0

        dead = os.path.join(base, "symmetria-nvim-dead")
        os.makedirs(dead)
        os.utime(dead, (old, old))  # aged out, no socket → orphan

        live = os.path.join(base, "symmetria-nvim-live")
        os.makedirs(live)
        os.utime(live, (old, old))  # aged out BUT a live listener spares it
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(os.path.join(live, "nvim.sock"))
        srv.listen(1)

        fresh = os.path.join(base, "symmetria-nvim-fresh")
        os.makedirs(fresh)  # < 60s old → spared by the age guard

        try:
            _reap_orphan_nvim_sockets()
            assert not os.path.exists(dead), "dead orphan should be reaped"
            assert os.path.isdir(live), "live instance dir must be spared"
            assert os.path.isdir(fresh), "mid-launch (fresh) dir must be spared"
        finally:
            srv.close()
            _rmtree(base)


def _rmtree(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


class TestConfigureRenderLoopForScreenshot:
    """Tests for _configure_render_loop_for_screenshot().

    The helper is called before QGuiApplication construction; these tests
    verify its env-var contract without starting Qt.
    """

    def test_sets_basic_when_screenshot_env_set(self, monkeypatch):
        """When SYMMETRIA_IDE_SCREENSHOT is set, QSG_RENDER_LOOP should become 'basic'."""
        monkeypatch.setenv("SYMMETRIA_IDE_SCREENSHOT", "/tmp/out.png")
        monkeypatch.delenv("QSG_RENDER_LOOP", raising=False)

        from symmetria_ide.app import _configure_render_loop_for_screenshot

        _configure_render_loop_for_screenshot()

        assert os.environ.get("QSG_RENDER_LOOP") == "basic"

    def test_no_op_when_screenshot_env_absent(self, monkeypatch):
        """Without SYMMETRIA_IDE_SCREENSHOT, QSG_RENDER_LOOP must not be touched."""
        monkeypatch.delenv("SYMMETRIA_IDE_SCREENSHOT", raising=False)
        monkeypatch.delenv("QSG_RENDER_LOOP", raising=False)

        from symmetria_ide.app import _configure_render_loop_for_screenshot

        _configure_render_loop_for_screenshot()

        assert os.environ.get("QSG_RENDER_LOOP") is None

    def test_explicit_caller_override_wins(self, monkeypatch):
        """An explicit QSG_RENDER_LOOP set by the caller must not be overwritten
        (setdefault semantics — the deadlock mitigation should never trump an
        intentional integration-test override like QSG_RENDER_LOOP=threaded)."""
        monkeypatch.setenv("SYMMETRIA_IDE_SCREENSHOT", "/tmp/out.png")
        monkeypatch.setenv("QSG_RENDER_LOOP", "threaded")

        from symmetria_ide.app import _configure_render_loop_for_screenshot

        _configure_render_loop_for_screenshot()

        assert os.environ.get("QSG_RENDER_LOOP") == "threaded"


class TestConfigureFreetypeInterpreter:
    """Tests for _configure_freetype_interpreter().

    The helper is called before QGuiApplication construction (FreeType is
    loaded during Qt init); these tests verify its env-var contract without
    starting Qt.
    """

    def test_sets_v35_interpreter(self, monkeypatch):
        """With no caller-set value, FREETYPE_PROPERTIES selects the v35
        interpreter (true two-axis stem hinting at fractional DPR)."""
        monkeypatch.delenv("FREETYPE_PROPERTIES", raising=False)

        from symmetria_ide.app import _configure_freetype_interpreter

        _configure_freetype_interpreter()

        assert os.environ.get("FREETYPE_PROPERTIES") == (
            "truetype:interpreter-version=35"
        )

    def test_explicit_caller_override_wins(self, monkeypatch):
        """A user-set FREETYPE_PROPERTIES must not be overwritten (setdefault
        semantics — system-wide font tuning beats our process default)."""
        monkeypatch.setenv("FREETYPE_PROPERTIES", "truetype:interpreter-version=40")

        from symmetria_ide.app import _configure_freetype_interpreter

        _configure_freetype_interpreter()

        assert os.environ.get("FREETYPE_PROPERTIES") == (
            "truetype:interpreter-version=40"
        )


class TestExportHostWindowPid:
    """Tests for _export_host_window_pid().

    Unlike _configure_freetype_interpreter and _configure_render_loop_for_screenshot,
    this helper uses unconditional assignment (not setdefault) — the IDE's own PID
    must always win, because child processes (nvim, shell) need to resolve back to
    the parent QGuiApplication window regardless of what the caller set.
    """

    def test_sets_pid_of_current_process(self, monkeypatch):
        """SYMMETRIA_HOST_WINDOW_PID is set to the current process PID as a string."""
        monkeypatch.delenv("SYMMETRIA_HOST_WINDOW_PID", raising=False)

        from symmetria_ide.app import _export_host_window_pid

        _export_host_window_pid()

        assert os.environ.get("SYMMETRIA_HOST_WINDOW_PID") == str(os.getpid())

    def test_overwrites_any_existing_value(self, monkeypatch):
        """Unlike the setdefault helpers, _export_host_window_pid unconditionally
        writes our PID — a stale value from a parent shell must not persist."""
        monkeypatch.setenv("SYMMETRIA_HOST_WINDOW_PID", "99999")

        from symmetria_ide.app import _export_host_window_pid

        _export_host_window_pid()

        assert os.environ.get("SYMMETRIA_HOST_WINDOW_PID") == str(os.getpid())
