"""Pure unit tests for `worktree` — linked git-worktree detection.

Fixtures are plain file writes (no git subprocess): git's on-disk contract
for a linked worktree is just a `.git` FILE containing
`gitdir: <main>/.git/worktrees/<name>`, so tests fabricate exactly that.
Mirrors test_agent_harness / test_agent_registry (no Qt imports).
"""

from __future__ import annotations

from symmetria_ide.worktree import canonical_project_root, linked_worktree_info


def _mkmain(base, name="proj"):
    """An ordinary main checkout: a dir with a real `.git` DIRECTORY."""
    root = base / name
    (root / ".git").mkdir(parents=True)
    return str(root)


def _mkworktree(base, main, wt_name="feature-x", dir_name=None, gitdir=None):
    """A linked-worktree checkout: a dir whose `.git` is a pointer FILE."""
    wt = base / (dir_name or f"proj-{wt_name}")
    wt.mkdir()
    target = gitdir or f"{main}/.git/worktrees/{wt_name}"
    (wt / ".git").write_text(f"gitdir: {target}\n", encoding="utf-8")
    return str(wt)


# ---------------------------------------------------------------------------
# linked_worktree_info
# ---------------------------------------------------------------------------


def test_absolute_gitdir_worktree(tmp_path):
    main = _mkmain(tmp_path)
    wt = _mkworktree(tmp_path, main)
    assert linked_worktree_info(wt) == (main, "feature-x")


def test_relative_gitdir_worktree(tmp_path):
    """git writes relative `gitdir:` targets in some configurations — they
    resolve against the worktree root itself."""
    main = _mkmain(tmp_path)
    wt = _mkworktree(tmp_path, main, gitdir="../proj/.git/worktrees/feature-x")
    assert linked_worktree_info(wt) == (main, "feature-x")


def test_main_checkout_is_not_a_worktree(tmp_path):
    """A real `.git` DIRECTORY (ordinary repo) is not a linked worktree."""
    main = _mkmain(tmp_path)
    assert linked_worktree_info(main) == ("", "")


def test_submodule_pointer_is_not_a_worktree(tmp_path):
    """Submodules also use a `.git` pointer file, but the target is
    `.git/modules/<name>` — must NOT count (different repository)."""
    main = _mkmain(tmp_path)
    sub = _mkworktree(tmp_path, main, dir_name="sub", gitdir=f"{main}/.git/modules/sub")
    assert linked_worktree_info(sub) == ("", "")


def test_missing_dotgit_is_not_a_worktree(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert linked_worktree_info(str(plain)) == ("", "")


def test_garbage_pointer_file_is_not_a_worktree(tmp_path):
    wt = tmp_path / "garbage"
    wt.mkdir()
    (wt / ".git").write_text("this is not a gitdir pointer\n")
    assert linked_worktree_info(str(wt)) == ("", "")


def test_empty_gitdir_target_is_not_a_worktree(tmp_path):
    wt = tmp_path / "emptyptr"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: \n")
    assert linked_worktree_info(str(wt)) == ("", "")


def test_empty_input(tmp_path):
    assert linked_worktree_info("") == ("", "")


def test_short_gitdir_target_rejected(tmp_path):
    """A target too shallow to contain <main>/.git/worktrees/<name>."""
    wt = tmp_path / "short"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /worktrees/x\n")
    assert linked_worktree_info(str(wt)) == ("", "")


# ---------------------------------------------------------------------------
# canonical_project_root
# ---------------------------------------------------------------------------


def test_canonical_root_of_worktree_is_main(tmp_path):
    main = _mkmain(tmp_path)
    wt = _mkworktree(tmp_path, main)
    assert canonical_project_root(wt) == main


def test_canonical_root_of_main_is_identity(tmp_path):
    main = _mkmain(tmp_path)
    assert canonical_project_root(main) == main


def test_canonical_root_stable_under_rerooting(tmp_path):
    """canonical(worktree) == canonical(main) — the property that makes it
    the right same-repo comparator while the chrome is re-rooted."""
    main = _mkmain(tmp_path)
    wt = _mkworktree(tmp_path, main)
    assert canonical_project_root(wt) == canonical_project_root(main)
