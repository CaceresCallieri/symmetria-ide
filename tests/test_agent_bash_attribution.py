"""Tests for Bash-write attribution (v2 per-agent change filter).

`bash_delta` is pure; `probe_dirty_leaves` shells out to real git against a
throwaway repo under `tmp_path` (no Qt, no QApplication). See
`src/symmetria_ide/agent_bash_attribution.py` and the Bash Pre/Post handling in
`AppController._on_bash_probe_ready`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from symmetria_ide.agent_bash_attribution import bash_delta, probe_dirty_leaves


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", "-C", root, *args], check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> str:
    """A realpath'd git repo with one committed file, clean working tree."""
    root = os.path.realpath(str(tmp_path))
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    (Path(root) / "committed.py").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def test_probe_finds_modified_and_untracked(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    (Path(root) / "committed.py").write_text("changed\n")  # modified (tracked)
    (Path(root) / "new.py").write_text("new\n")  # untracked
    leaves = probe_dirty_leaves(root)
    assert os.path.realpath(os.path.join(root, "committed.py")) in leaves
    assert os.path.realpath(os.path.join(root, "new.py")) in leaves


def test_probe_clean_repo_is_empty(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    assert probe_dirty_leaves(root) == set()


def test_probe_non_repo_and_blank_are_empty(tmp_path: Path) -> None:
    # A directory that is not a git repo → git status fails → empty set.
    assert probe_dirty_leaves(os.path.realpath(str(tmp_path))) == set()
    assert probe_dirty_leaves("") == set()


def test_bash_delta_keeps_only_newly_dirty(tmp_path: Path) -> None:
    pre = {"/r/a", "/r/b"}
    post = {"/r/b", "/r/c", "/r/d"}
    # c, d newly dirty (the command wrote them); a left post (reverted) → dropped;
    # b was already dirty before the command → not attributed.
    assert bash_delta(pre, post) == {"/r/c", "/r/d"}


def test_bash_delta_empty_when_nothing_new(tmp_path: Path) -> None:
    assert bash_delta({"/r/a"}, {"/r/a"}) == set()
    assert bash_delta(set(), set()) == set()
    # Everything cleaned up during the command → no NEW dirty files.
    assert bash_delta({"/r/a", "/r/b"}, set()) == set()


def test_probe_result_matches_delta_end_to_end(tmp_path: Path) -> None:
    """Pre (clean) → write two files → Post: the delta is exactly the writes."""
    root = _init_repo(tmp_path)
    pre = probe_dirty_leaves(root)  # clean
    (Path(root) / "committed.py").write_text("edited\n")
    (Path(root) / "gen_output.txt").write_text("generated\n")
    post = probe_dirty_leaves(root)
    assert bash_delta(pre, post) == {
        os.path.realpath(os.path.join(root, "committed.py")),
        os.path.realpath(os.path.join(root, "gen_output.txt")),
    }
