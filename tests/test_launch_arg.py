"""Tests for the `symmetria-ide [PATH]` launch-argument resolver.

`_apply_project_arg` chdir's into an optional project directory before
QGuiApplication / AppController spin up, so the whole stack (editor nvim,
shell, file tree, git pane) opens on the right project via the process cwd.
It must also pass Qt flags through untouched and never abort on a bad path.
"""

from __future__ import annotations

import os

import pytest

from symmetria_ide.app import _apply_project_arg


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
