"""End-to-end test of the per-agent change filter (v1 write-tool + v2 Bash).

Composes the REAL pieces — a real git repo, the real porcelain parser
(`parse_porcelain_v2`), the real Bash-probe (`probe_dirty_leaves`), the real
Pre/Post correlation (`AppController._on_bash_probe_ready`), the real
touched∩dirty fold, and the real QML-facing properties — to prove that BOTH
provenance sources land in the "this agent" scope and that a NON-agent change
is excluded. The async pool → queued-signal hop is standard Qt (covered by the
unit tests); per the project's no-processEvents-in-tests rule the probe results
are hand-delivered here.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from symmetria_ide.agent_bash_attribution import probe_dirty_leaves
from symmetria_ide.app import AppController
from symmetria_ide.git_controller import parse_porcelain_v2


@pytest.fixture
def controller():
    # A bare AppController — no spawn, no bridge, no start(): the slot record is
    # built directly and the GitController's status map is fed real git output.
    return AppController()


def _init_repo(tmp_path: Path) -> str:
    root = os.path.realpath(str(tmp_path))

    def g(*a: str) -> None:
        subprocess.run(["git", "-C", root, *a], check=True, capture_output=True)

    g("init", "-q")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "Test")
    (Path(root) / "committed.py").write_text("x\n")
    g("add", "-A")
    g("commit", "-qm", "init")
    return root


def _feed_git_status(controller: AppController, root: str) -> None:
    """Point the GitController at the repo's REAL current dirty state."""
    blob = subprocess.run(
        ["git", "-C", root, "status", "--porcelain=v2", "-z"], capture_output=True
    ).stdout
    gc = controller._git_controller
    with gc._lock:
        gc._status_map = parse_porcelain_v2(blob)
        gc._resolved_root = root


def test_e2e_this_agent_scope_from_edit_and_bash(controller, tmp_path):
    root = _init_repo(tmp_path)
    # A focused agent rooted at the repo (record built directly — no spawn).
    # `_bash_gen` mirrors what `_submit_bash_probe`'s Pre edge would have set.
    controller._term_agents[1] = {
        "cwd": root,
        "work_root": root,
        "location": "local",
        "_bash_gen": 1,
    }
    controller._focused_term_agent = 1

    # Three dirty files; only two are the agent's work.
    (Path(root) / "edited.py").write_text("agent edit\n")  # v1 write-tool
    (Path(root) / "bash_out.txt").write_text("generated\n")  # v2 bash write
    (Path(root) / "user.py").write_text("not the agent\n")  # user, not attributed
    _feed_git_status(controller, root)

    # v1 provenance: the write-tool touched edited.py.
    controller._term_agents[1].setdefault("touched", set()).add(
        os.path.realpath(os.path.join(root, "edited.py"))
    )
    # v2 provenance: REAL Bash snapshot-diff through the real correlation code.
    # pre = the dirty set before the command wrote bash_out.txt; post = now.
    post = probe_dirty_leaves(root)
    pre = post - {os.path.realpath(os.path.join(root, "bash_out.txt"))}
    controller._on_bash_probe_ready(1, 1, "pre", pre)
    controller._on_bash_probe_ready(1, 1, "post", post)

    # The "this agent" scope = edited.py + bash_out.txt; user.py excluded.
    path_set = controller.focusedAgentChangesPathSet
    assert os.path.join(root, "edited.py") in path_set
    assert os.path.join(root, "bash_out.txt") in path_set
    assert os.path.join(root, "user.py") not in path_set
    assert controller.focusedAgentChangesCount == 2
    # Sanity: the WHOLE-repo scope still includes the non-agent file.
    assert os.path.join(root, "user.py") in controller._git_controller.changedPathSet


def test_e2e_bash_only_agent_is_still_attributed(controller, tmp_path):
    """An agent that ONLY ran a Bash write (no Edit/Write tool) still shows in
    scope — exactly the v1 blind spot v2 closes."""
    root = _init_repo(tmp_path)
    controller._term_agents[1] = {
        "cwd": root,
        "work_root": root,
        "location": "local",
        "_bash_gen": 1,
    }
    controller._focused_term_agent = 1

    (Path(root) / "gen.txt").write_text("bash generated\n")
    _feed_git_status(controller, root)

    # Clean before the command; the command created gen.txt.
    post = probe_dirty_leaves(root)
    controller._on_bash_probe_ready(1, 1, "pre", set())
    controller._on_bash_probe_ready(1, 1, "post", post)

    assert os.path.join(root, "gen.txt") in controller.focusedAgentChangesPathSet
    assert controller.focusedAgentChangesCount == 1


def test_e2e_unfocused_agent_and_no_agent_are_empty(controller, tmp_path):
    """The scope is the FOCUSED agent's; an unfocused agent's touched set does
    not leak, and no focused agent yields an empty scope."""
    root = _init_repo(tmp_path)
    (Path(root) / "gen.txt").write_text("x\n")
    _feed_git_status(controller, root)

    # Agent in slot 2 touched gen.txt, but slot 1 is focused (empty).
    controller._term_agents[2] = {
        "cwd": root,
        "touched": {os.path.realpath(os.path.join(root, "gen.txt"))},
    }
    controller._focused_term_agent = 1
    assert controller.focusedAgentChangesPathSet == {}
    assert controller.focusedAgentChangesCount == 0

    # No focused agent at all.
    controller._focused_term_agent = 0
    assert controller.focusedAgentChangesPathSet == {}
    assert controller.focusedAgentChangesCount == 0
