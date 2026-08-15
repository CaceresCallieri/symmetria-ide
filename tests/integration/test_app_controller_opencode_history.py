"""Phase-5 RPC-level guards for OpenCode history, picker, and resume."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from symmetria_ide import app as app_module
from symmetria_ide.agent_threads import OpenCodeThreadReader, ThreadRecord
from symmetria_ide.app import AppController


class _Bridge:
    def notify_spawn(self, _instance: dict) -> None:
        pass

    def notify_remove(self, _slot: int) -> None:
        pass

    def notify_focus(self, _slot: int) -> None:
        pass

    def notify_title(self, _slot: int, _title: str) -> None:
        pass

    def notify_activity(self, _slot: int, **_activity: object) -> None:
        pass

    def stop(self) -> None:
        pass


def _repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


@pytest.fixture
def controller(monkeypatch):
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_PROMPT", raising=False)
    monkeypatch.delenv("SYMMETRIA_IDE_AGENT_VIEW", raising=False)
    monkeypatch.setattr(
        "symmetria_ide.app.shutil.which", lambda executable: f"/bin/{executable}"
    )
    value = AppController()
    value._agent_bridge = _Bridge()
    yield value
    value.shutdown()


def _set_project(controller: AppController, root: Path) -> None:
    controller._route_capsule({"id": "cwd", "label": "", "value": str(root)})


def _publish_opencode_thread(
    controller: AppController,
    project: Path,
    *,
    session_id: str,
    title: str,
    work_root: str = "",
) -> None:
    controller._on_threads_indexed(
        str(project),
        "opencode",
        [
            ThreadRecord(
                harness="opencode",
                session_id=session_id,
                title=title,
                updated_at=1_755_000_000,
                session_cwd=str(project),
                work_root=work_root,
                resumable=True,
            )
        ],
    )


def _record(
    project: Path,
    *,
    session_id: str,
    title: str,
    updated_at: int = 1_755_000_000,
) -> ThreadRecord:
    return ThreadRecord(
        harness="opencode",
        session_id=session_id,
        title=title,
        updated_at=updated_at,
        session_cwd=str(project),
        work_root="",
        resumable=True,
    )


class _ImmediateThread:
    """Run a one-shot worker inline without pumping the shared Qt event loop."""

    def __init__(self, *, target, args=(), **_kwargs) -> None:
        self._target = target
        self._args = args

    def start(self) -> None:
        self._target(*self._args)


def test_indexed_opencode_without_work_root_resumes_main_with_visible_notice(
    controller: AppController, tmp_path
) -> None:
    """Guard: unknown legacy location falls back visibly, never silently."""
    main = _repo(tmp_path / "project")
    _set_project(controller, main)
    _publish_opencode_thread(
        controller,
        main,
        session_id="ses-no-work-root",
        title="Legacy OpenCode thread",
    )
    notices: list[tuple[str, str]] = []
    controller.locationAlert.connect(
        lambda title, detail: notices.append((title, detail))
    )

    controller.resume_thread("opencode:ses-no-work-root")

    resumed = controller._term_agents[controller.focusedAgent]
    assert resumed["spawn_type"] == "resume"
    assert resumed["harness"] == "opencode"
    assert resumed["session_id"] == "ses-no-work-root"
    assert resumed["cwd"] == str(main)
    assert notices and all(title and detail for title, detail in notices)


def test_session_picker_shows_cached_rows_then_refreshes_shared_index(
    controller: AppController, monkeypatch, tmp_path
) -> None:
    """Guard: sessions created after startup become pickable on the next open."""
    main = _repo(tmp_path / "project")
    _set_project(controller, main)
    _publish_opencode_thread(
        controller,
        main,
        session_id="ses-startup",
        title="Indexed at startup",
    )
    monkeypatch.setenv("SYMMETRIA_IDE_THREAD_INDEX", "1")
    # Prove the user action bypasses the ordinary same-project startup guard.
    controller._thread_index_root = str(main)
    index_requests: list[str] = []
    monkeypatch.setattr(
        controller._agent_thread_indexer, "start", index_requests.append
    )
    payloads: list[dict] = []
    controller.opencodeSessionsReady.connect(payloads.append)

    controller.request_opencode_sessions()

    assert index_requests == [str(main)]
    assert [row["id"] for row in payloads[0]["sessions"]] == ["ses-startup"]

    controller._on_threads_indexed(
        str(main),
        "opencode",
        [
            _record(
                main,
                session_id="ses-created-later",
                title="Created after startup",
                updated_at=1_755_000_100,
            ),
            _record(
                main,
                session_id="ses-startup",
                title="Indexed at startup",
            ),
        ],
    )

    assert [row["id"] for row in payloads[-1]["sessions"]] == [
        "ses-created-later",
        "ses-startup",
    ]


def test_empty_refresh_does_not_erase_picker_rows_already_shown(
    controller: AppController, monkeypatch, tmp_path
) -> None:
    """Guard: an ambiguous empty scan cannot masquerade as no sessions."""
    main = _repo(tmp_path / "project")
    _set_project(controller, main)
    _publish_opencode_thread(
        controller,
        main,
        session_id="ses-visible",
        title="Keep visible on a transient failure",
    )
    monkeypatch.setenv("SYMMETRIA_IDE_THREAD_INDEX", "1")
    controller._thread_index_root = str(main)
    monkeypatch.setattr(controller._agent_thread_indexer, "start", lambda _root: None)
    payloads: list[dict] = []
    controller.opencodeSessionsReady.connect(payloads.append)

    controller.request_opencode_sessions()
    controller._on_threads_indexed(str(main), "opencode", [])

    assert len(payloads) == 1
    assert [row["id"] for row in payloads[0]["sessions"]] == ["ses-visible"]


def test_empty_index_cache_falls_back_to_picker_error_result(
    controller: AppController, monkeypatch, tmp_path
) -> None:
    """Guard: a failed cold query renders failure, not a successful empty list."""
    main = _repo(tmp_path / "project")
    _set_project(controller, main)
    controller._on_threads_indexed(str(main), "opencode", [])
    calls: list[tuple[list[str], dict]] = []

    def failed_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 7, stdout="", stderr="refused")

    payloads: list[dict] = []
    controller.opencodeSessionsReady.connect(payloads.append)

    def deliver(_signal, payload) -> None:
        # Hand-deliver the queued worker result; pumping the suite's shared Qt
        # event loop is forbidden by tests/conftest.py's crash invariant.
        controller._on_opencode_sessions(payload)

    with monkeypatch.context() as patch:
        patch.setattr(app_module.threading, "Thread", _ImmediateThread)
        patch.setattr(app_module.subprocess, "run", failed_run)
        patch.setattr(app_module, "emit_gc_safe", deliver)
        controller.request_opencode_sessions()

    assert len(calls) == 1
    assert payloads == [{"ok": False, "sessions": []}]


def test_picker_cold_path_filters_foreign_project_rows_and_shares_reader_query(
    controller: AppController, monkeypatch, tmp_path
) -> None:
    """Guard: the fallback cannot offer another project's conversation."""
    main = _repo(tmp_path / "project")
    foreign = _repo(tmp_path / "foreign")
    monkeypatch.setenv("SYMMETRIA_IDE_OPENCODE_BIN", "/test/fake-opencode")
    stdout = json.dumps(
        [
            {
                "id": "ses-local",
                "title": "This project",
                "directory": str(main),
                "projectId": "local",
                "updated": 3_000,
                "created": 1_000,
            },
            {
                "id": "ses-foreign",
                "title": "Other project",
                "directory": str(foreign),
                "projectId": "global",
                "updated": 4_000,
                "created": 1_000,
            },
        ]
    )
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    captured: list[dict] = []
    with monkeypatch.context() as patch:
        patch.setattr(app_module.subprocess, "run", fake_run)
        patch.setattr(
            app_module,
            "emit_gc_safe",
            lambda _signal, payload: captured.append(payload),
        )
        controller._fetch_opencode_sessions(str(main))

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == OpenCodeThreadReader().list_argv()
    assert kwargs["cwd"] == str(main)
    assert kwargs["timeout"] == OpenCodeThreadReader.TIMEOUT_SEC
    assert captured[0]["ok"] is True
    assert [row["id"] for row in captured[0]["sessions"]] == ["ses-local"]
