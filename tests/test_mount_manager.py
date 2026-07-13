"""mount_manager — sshfs argv, mountpoint layout, and the SshfsMount state
machine (subprocess-free; the worker's queued GUI slot is hand-delivered per
the no-event-pumping rule, gotcha #10 / processevents_shared_app_segv)."""

from __future__ import annotations

import subprocess
import time

from PySide6.QtCore import Qt

from symmetria_ide import mount_manager
from symmetria_ide.mount_manager import SshfsMount, mountpoint_for, sshfs_argv
from symmetria_ide.server_registry import RemoteServer

SERVER = RemoteServer(name="vps", host="203.0.113.7")
REMOTE = "/opt/dev/repos/demo"


def test_mountpoint_under_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert mountpoint_for(SERVER, REMOTE) == str(
        tmp_path / "symmetria-ide" / "mnt" / "vps" / "demo"
    )


def test_sshfs_argv_shape():
    argv = sshfs_argv(SERVER, REMOTE, "/mnt/x")
    assert argv[0] == "sshfs"
    assert argv[-2] == f"dev@203.0.113.7:{REMOTE}"
    assert argv[-1] == "/mnt/x"
    options = argv[argv.index("-o") + 1].split(",")
    # reconnect is THE reason sshfs owns its connection (no ControlMaster).
    assert "reconnect" in options
    assert "IdentitiesOnly=yes" in options
    assert "BatchMode=yes" in options
    assert any(option.startswith("IdentityFile=") for option in options)
    assert not any("ControlMaster" in option for option in options)


def _spy_finished(mount: SshfsMount) -> list[tuple]:
    payloads: list[tuple] = []
    # Explicit direct connection — the worker thread's emit must land in
    # the spy synchronously (no event pumping in tests).
    mount._mountFinished.connect(
        lambda *args: payloads.append(args), Qt.ConnectionType.DirectConnection
    )
    return payloads


def _wait_for(payloads: list, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not payloads and time.monotonic() < deadline:
        time.sleep(0.01)
    assert payloads, "mount worker did not report"


def test_mount_success_path(monkeypatch, tmp_path, qt_app):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(mount_manager, "_is_mounted", lambda mp: False)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        mount_manager.subprocess,
        "run",
        lambda argv, **kw: (
            calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")
        ),
    )
    mount = SshfsMount(SERVER, REMOTE)
    payloads = _spy_finished(mount)
    states: list[str] = []
    mount.stateChanged.connect(lambda: states.append(mount.state))
    mount.mount()
    assert mount.state == "mounting"
    _wait_for(payloads)
    mount._on_mount_finished(*payloads[0])  # hand-deliver the queued slot
    assert mount.state == "mounted"
    assert mount.mounted is True
    assert calls and calls[0][0] == "sshfs"
    assert states == ["mounting", "mounted"]


def test_mount_failure_sets_failed(monkeypatch, tmp_path, qt_app):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(mount_manager, "_is_mounted", lambda mp: False)
    monkeypatch.setattr(
        mount_manager.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 1, "", "read: Connection reset by peer"
        ),
    )
    mount = SshfsMount(SERVER, REMOTE)
    payloads = _spy_finished(mount)
    mount.mount()
    _wait_for(payloads)
    mount._on_mount_finished(*payloads[0])
    assert mount.state == "failed"
    assert mount.mounted is False


def test_mount_reuses_live_mount_without_subprocess(monkeypatch, tmp_path, qt_app):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(mount_manager, "_is_mounted", lambda mp: True)

    def _fail_run(*args, **kwargs):  # pragma: no cover - assertion vehicle
        raise AssertionError("a live mount must be reused, not re-run")

    monkeypatch.setattr(mount_manager.subprocess, "run", _fail_run)
    mount = SshfsMount(SERVER, REMOTE)
    payloads = _spy_finished(mount)
    mount.mount()
    _wait_for(payloads)
    mount._on_mount_finished(*payloads[0])
    assert mount.state == "mounted"


def test_stale_worker_result_discarded_after_unmount(monkeypatch, tmp_path, qt_app):
    """A mount result landing after unmount() must not resurrect 'mounted'."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(mount_manager, "_is_mounted", lambda mp: False)
    monkeypatch.setattr(
        mount_manager.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
    )
    mount = SshfsMount(SERVER, REMOTE)
    payloads = _spy_finished(mount)
    mount.mount()
    _wait_for(payloads)
    mount.unmount()  # bumps the generation
    assert mount.state == "unmounted"
    mount._on_mount_finished(*payloads[0])  # stale generation → discarded
    assert mount.state == "unmounted"


def test_unmount_falls_back_to_lazy(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    # Stateful fake: mounted until a fusermount call succeeds (unmount()
    # re-checks /proc/mounts after its attempts before trusting the state).
    still_mounted = {"value": True}
    monkeypatch.setattr(mount_manager, "_is_mounted", lambda mp: still_mounted["value"])
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        # First (plain) unmount fails EBUSY; the lazy retry succeeds.
        if len(calls) > 1:
            still_mounted["value"] = False
        return subprocess.CompletedProcess(argv, 1 if len(calls) == 1 else 0, "", "")

    monkeypatch.setattr(mount_manager.subprocess, "run", fake_run)
    mount = SshfsMount(SERVER, REMOTE)
    mount.unmount()
    assert [argv[:2] for argv in calls] == [
        ["fusermount3", "-u"],
        ["fusermount3", "-u"],
    ]
    assert "-z" in calls[1]
    assert mount.state == "unmounted"


def test_unmount_reports_failed_when_mount_survives(monkeypatch, tmp_path):
    """If both fusermount attempts fail, the state must not lie 'unmounted'."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(mount_manager, "_is_mounted", lambda mp: True)
    monkeypatch.setattr(
        mount_manager.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", "busy"),
    )
    mount = SshfsMount(SERVER, REMOTE)
    mount.unmount()
    assert mount.state == "failed"


def test_healthy_false_when_not_in_proc_mounts(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(mount_manager, "_is_mounted", lambda mp: False)
    assert SshfsMount(SERVER, REMOTE).healthy() is False


def test_healthy_true_when_stat_answers(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(mount_manager, "_is_mounted", lambda mp: True)
    monkeypatch.setattr(mount_manager.os, "stat", lambda path: None)
    assert SshfsMount(SERVER, REMOTE).healthy() is True
