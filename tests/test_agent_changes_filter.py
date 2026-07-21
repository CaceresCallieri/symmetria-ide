"""Pure-logic tests for the per-agent change filter (v1 side-panel scope).

Exercises `_fold_agent_changes` — the projection behind
`GitController.changed_path_set_for` — with no Qt instantiation (no
QApplication). Real files under `tmp_path` back the `os.path.realpath`
membership test so the fold is deterministic. See the "este agente" scope in
`qml/GitStatusPanel.qml` and `AppController.focusedAgentChangesPathSet`.
"""

from __future__ import annotations

import os
from pathlib import Path

from symmetria_ide.git_controller import GitStatus, _fold_agent_changes


def _mod(rel: str) -> GitStatus:
    """A dirty (modified, unstaged) leaf entry."""
    return GitStatus(path=rel, char="M", state="unstaged", tooltip="Modified")


def _dir(rel: str) -> GitStatus:
    """An ancestor-dir aggregate (char '·'), as `_add_directory_aggregates`
    synthesizes — must never be counted as a leaf."""
    return GitStatus(path=rel, char="·", state="unstaged", tooltip="")


def _make_repo(tmp_path: Path) -> str:
    """Realpath'd repo root with the fixture files created on disk."""
    root = os.path.realpath(str(tmp_path))
    for rel in (
        "src/foo.py",
        "src/bar.py",
        "README.md",
        "docs/guide.md",
        "src/a/b/c.py",
    ):
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    return root


def _abs(root: str, rel: str) -> str:
    return os.path.join(root, rel)


def test_fold_intersects_touched_with_dirty(tmp_path: Path) -> None:
    """Only touched-AND-dirty leaves survive; their ancestors + root come along;
    untouched dirty files and their dirs are excluded."""
    root = _make_repo(tmp_path)
    status_map = {
        "src/foo.py": _mod("src/foo.py"),
        "src/bar.py": _mod("src/bar.py"),
        "README.md": _mod("README.md"),
        "docs/guide.md": _mod("docs/guide.md"),
        "src": _dir("src"),
        "docs": _dir("docs"),
    }
    # Agent touched only foo.py and README.md.
    keep_real = {
        os.path.realpath(_abs(root, "src/foo.py")),
        os.path.realpath(_abs(root, "README.md")),
    }
    out, count = _fold_agent_changes(status_map, root, keep_real)

    assert count == 2  # leaves only, dirs excluded from the count
    assert out[root] is True
    assert out[_abs(root, "src")] is True
    assert out[_abs(root, "src/foo.py")] is True
    assert out[_abs(root, "README.md")] is True
    # Untouched dirty file (and the dir it alone would justify) must be absent.
    assert _abs(root, "src/bar.py") not in out
    assert _abs(root, "docs") not in out
    assert _abs(root, "docs/guide.md") not in out


def test_fold_includes_full_ancestor_chain(tmp_path: Path) -> None:
    """A deeply nested leaf pulls in every intermediate dir up to the root."""
    root = _make_repo(tmp_path)
    status_map = {"src/a/b/c.py": _mod("src/a/b/c.py")}
    keep_real = {os.path.realpath(_abs(root, "src/a/b/c.py"))}
    out, count = _fold_agent_changes(status_map, root, keep_real)

    assert count == 1
    for rel in ("", "src", "src/a", "src/a/b", "src/a/b/c.py"):
        key = root if rel == "" else _abs(root, rel)
        assert out.get(key) is True, f"missing ancestor {key!r}"


def test_fold_touched_but_clean_file_yields_empty(tmp_path: Path) -> None:
    """A file the agent touched that is NOT in the dirty map keeps nothing —
    the fold collapses to ({}, 0), driving the empty-state (not a lone root)."""
    root = _make_repo(tmp_path)
    status_map = {"src/foo.py": _mod("src/foo.py"), "src": _dir("src")}
    # Agent touched bar.py, which is clean (absent from status_map).
    keep_real = {os.path.realpath(_abs(root, "src/bar.py"))}
    assert _fold_agent_changes(status_map, root, keep_real) == ({}, 0)


def test_fold_empty_and_degenerate_inputs(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    status_map = {"src/foo.py": _mod("src/foo.py"), "src": _dir("src")}
    keep = {os.path.realpath(_abs(root, "src/foo.py"))}

    assert _fold_agent_changes(status_map, root, set()) == ({}, 0)  # empty keep
    assert _fold_agent_changes(status_map, "", keep) == ({}, 0)  # no repo
    assert _fold_agent_changes({}, root, keep) == ({}, 0)  # clean tree


def test_fold_never_counts_dir_aggregate_as_leaf(tmp_path: Path) -> None:
    """Even if a '·' aggregate's path is (pathologically) in keep, it is skipped
    as a leaf — only real files count."""
    root = _make_repo(tmp_path)
    status_map = {"src": _dir("src")}
    keep_real = {os.path.realpath(_abs(root, "src"))}
    assert _fold_agent_changes(status_map, root, keep_real) == ({}, 0)


def test_fold_resolves_touched_path_through_symlink(tmp_path: Path) -> None:
    """A tool_path captured through a symlinked dir realpaths to the canonical
    leaf and still matches git's rel key — the realpath rationale of the fold."""
    root = _make_repo(tmp_path)  # has src/foo.py
    link = Path(root) / "link"
    link.symlink_to(Path(root) / "src")  # link/foo.py -> src/foo.py
    status_map = {"src/foo.py": _mod("src/foo.py"), "src": _dir("src")}
    # The agent edited the file via the symlink; `touched` stores its realpath.
    touched = {os.path.realpath(str(link / "foo.py"))}
    out, count = _fold_agent_changes(status_map, root, touched)
    assert count == 1
    assert out[_abs(root, "src/foo.py")] is True
    assert out[_abs(root, "src")] is True


def test_fold_dedupes_shared_ancestor(tmp_path: Path) -> None:
    """Two leaves under a common dir yield that ancestor exactly once."""
    root = _make_repo(tmp_path)  # has src/foo.py, src/bar.py
    status_map = {
        "src/foo.py": _mod("src/foo.py"),
        "src/bar.py": _mod("src/bar.py"),
        "src": _dir("src"),
    }
    keep_real = {
        os.path.realpath(_abs(root, "src/foo.py")),
        os.path.realpath(_abs(root, "src/bar.py")),
    }
    out, count = _fold_agent_changes(status_map, root, keep_real)
    assert count == 2
    # Exactly: root + src (once) + the two leaves — no duplicate ancestor rows.
    assert set(out) == {
        root,
        _abs(root, "src"),
        _abs(root, "src/foo.py"),
        _abs(root, "src/bar.py"),
    }
