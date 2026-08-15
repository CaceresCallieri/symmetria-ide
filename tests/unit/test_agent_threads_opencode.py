"""Phase-5 guards for the project-scoped OpenCode thread reader."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from pathlib import Path

import pytest

from symmetria_ide import agent_harness, agent_threads


def _repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def _linked_worktree(main: Path, path: Path, name: str = "feature") -> Path:
    (main / ".git" / "worktrees" / name).mkdir(parents=True, exist_ok=True)
    path.mkdir(parents=True)
    (path / ".git").write_text(
        f"gitdir: {main}/.git/worktrees/{name}\n", encoding="utf-8"
    )
    return path


def _field(row: object, name: str):
    if isinstance(row, dict):
        return row[name]
    return getattr(row, name)


def test_opencode_reader_runs_exact_list_command_at_canonical_project_root(
    monkeypatch, tmp_path
) -> None:
    """Guard: discovery stays rooted at main for a linked worktree."""
    monkeypatch.delenv("SYMMETRIA_IDE_OPENCODE_BIN")
    main = _repo(tmp_path / "project")
    worktree = _linked_worktree(main, tmp_path / "project-feature")
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")

    reader_type = agent_threads.OpenCodeThreadReader
    monkeypatch.setattr(agent_threads.subprocess, "run", fake_run)

    assert reader_type().read(str(worktree), threading.Event()) == []
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["opencode", "session", "list", "--format", "json"]
    assert kwargs["cwd"] == str(main)


def test_opencode_reader_defaults_to_opencode_executable(monkeypatch) -> None:
    """Guard: suite isolation does not replace the production executable default."""
    monkeypatch.delenv("SYMMETRIA_IDE_OPENCODE_BIN")

    assert agent_threads.OpenCodeThreadReader().list_argv()[0] == "opencode"


def test_opencode_reader_is_globally_isolated_from_the_real_cli() -> None:
    """Guard: an unmocked reader cannot reach the developer's session store."""
    executable = Path(agent_threads.OpenCodeThreadReader().list_argv()[0])

    assert executable.is_absolute()
    assert not executable.exists()


def test_opencode_reader_keeps_only_directories_in_the_canonical_project(
    monkeypatch, tmp_path
) -> None:
    """Guard: a global-scope response cannot leak another project's sessions."""
    main = _repo(tmp_path / "project")
    worktree = _linked_worktree(main, tmp_path / "project-feature")
    foreign = _repo(tmp_path / "foreign")
    payload = json.dumps(
        [
            {
                "id": "ses-main",
                "title": "Main checkout",
                "directory": str(main),
                "projectId": "project-one",
                "updated": 3_000,
                "created": 1_000,
            },
            {
                "id": "ses-worktree",
                "title": "Linked worktree",
                "directory": str(worktree),
                "projectId": "project-one",
                "updated": 2_000,
                "created": 1_000,
            },
            {
                "id": "ses-foreign",
                "title": "Must not leak",
                "directory": str(foreign),
                "projectId": "project-two",
                "updated": 4_000,
                "created": 1_000,
            },
            {
                "id": "ses-no-directory",
                "title": "Unscoped rows must not leak",
                "projectId": "project-one",
                "updated": 5_000,
                "created": 1_000,
            },
        ]
    )

    def fake_run(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")

    reader_type = agent_threads.OpenCodeThreadReader
    monkeypatch.setattr(agent_threads.subprocess, "run", fake_run)

    rows = reader_type().read(str(main), threading.Event())

    assert {_field(row, "session_id") for row in rows} == {
        "ses-main",
        "ses-worktree",
    }
    assert {_field(row, "session_cwd") for row in rows} == {
        str(main),
        str(worktree),
    }
    assert all(_field(row, "work_root") == "" for row in rows)


@pytest.mark.parametrize(
    ("case", "outcome"),
    [
        ("missing binary", FileNotFoundError("opencode not found")),
        (
            "timeout",
            subprocess.TimeoutExpired(
                ["opencode", "session", "list", "--format", "json"], 15
            ),
        ),
        (
            "nonzero exit",
            subprocess.CompletedProcess(
                ["opencode"], 2, stdout="", stderr="project lookup failed"
            ),
        ),
        (
            "invalid json",
            subprocess.CompletedProcess(["opencode"], 0, stdout="not-json", stderr=""),
        ),
    ],
)
def test_opencode_reader_failures_are_empty_and_logged(
    monkeypatch, caplog, tmp_path, case, outcome
) -> None:
    """Guard: every expected CLI failure stays contained inside the reader."""
    project = _repo(tmp_path / "project")

    def fake_run(_argv, **_kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    reader_type = agent_threads.OpenCodeThreadReader
    monkeypatch.setattr(agent_threads.subprocess, "run", fake_run)
    caplog.set_level(logging.WARNING)

    assert reader_type().read(str(project), threading.Event()) == [], case
    assert any(
        record.levelno >= logging.WARNING and "opencode" in record.getMessage().lower()
        for record in caplog.records
    ), f"{case} was not logged"


def test_opencode_parser_preserves_session_scope_and_epoch_metadata() -> None:
    """Guard: shared parsed rows retain every field the index reader needs."""
    payload = json.dumps(
        [
            {
                "id": "ses-metadata",
                "title": "Metadata survives",
                "directory": "/project/feature",
                "projectId": "prj-123",
                "updated": 1_755_000_000_123,
                "created": 1_754_000_000_456,
            }
        ]
    )

    rows = agent_harness.parse_opencode_sessions(payload)

    assert rows is not None
    assert len(rows) == 1
    assert {
        key: rows[0][key]
        for key in ("id", "title", "directory", "projectId", "updated", "created")
    } == {
        "id": "ses-metadata",
        "title": "Metadata survives",
        "directory": "/project/feature",
        "projectId": "prj-123",
        "updated": 1_755_000_000_123,
        "created": 1_754_000_000_456,
    }


def test_default_reader_registry_indexes_claude_and_opencode() -> None:
    """Guard: OpenCode remains in the pluggable history-reader registry."""
    assert [reader.harness for reader in agent_threads.default_readers()] == [
        "claude",
        "opencode",
    ]
