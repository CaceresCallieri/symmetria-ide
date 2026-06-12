"""Tests for the recursive working-tree watcher + GitController triggers.

Three layers:
  1. Pure filtering logic (`_is_git_internal`, `handle_fs_event`) with
     fake watchdog events — no observer threads, no disk.
  2. `GitController.is_path_ignored` / `poke` — the controller-side seams
     the watcher and the nvim `gitpoke` capsule hang off.
  3. One real-observer integration test against a tmp dir, verifying the
     end-to-end inotify path (create file → `changed` emit).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import pytest
from PySide6.QtCore import Qt

from symmetria_ide.git_controller import GitController
from symmetria_ide.worktree_watcher import (
    WATCHDOG_AVAILABLE,
    WorktreeWatcher,
    _is_git_internal,
)


@dataclass
class FakeEvent:
    """Duck-typed stand-in for a watchdog FileSystemEvent."""

    event_type: str
    src_path: str
    dest_path: str = ""


@dataclass
class EmitRecorder:
    """Collects `changed` emits; cross-thread-safe for the observer test."""

    count: int = 0
    fired: threading.Event = field(default_factory=threading.Event)

    def on_changed(self) -> None:
        self.count += 1
        self.fired.set()


def make_watcher(
    ignored: set[str] | None = None,
) -> tuple[WorktreeWatcher, EmitRecorder]:
    ignored_set = ignored or set()
    watcher = WorktreeWatcher(lambda p: p in ignored_set)
    recorder = EmitRecorder()
    # DirectConnection in-thread: handle_fs_event is invoked synchronously
    # from the test, so the slot runs inline and `count` is immediately
    # readable. The QueuedConnection requirement applies to the production
    # connect site (GitController), not to this harness.
    watcher.changed.connect(recorder.on_changed)
    return watcher, recorder


def fresh_throttle(watcher: WorktreeWatcher) -> None:
    """Reset the emitter-thread throttle so each case stands alone."""
    watcher._last_emit_monotonic = 0.0


# ---------------------------------------------------------------------------
# _is_git_internal
# ---------------------------------------------------------------------------


def test_git_dir_itself_is_internal() -> None:
    assert _is_git_internal("/repo/.git")


def test_path_inside_git_dir_is_internal() -> None:
    assert _is_git_internal("/repo/.git/index.lock")


def test_ordinary_path_is_not_internal() -> None:
    assert not _is_git_internal("/repo/src/main.py")


def test_gitignore_file_is_not_internal() -> None:
    # `.gitignore` shares the `.git` prefix but is a real worktree file.
    assert not _is_git_internal("/repo/.gitignore")


def test_dotgit_named_subdir_content_is_internal() -> None:
    # Submodule layouts place a `.git` FILE in the subdir; events on it
    # are git-internal churn either way.
    assert _is_git_internal("/repo/vendor/.git")


# ---------------------------------------------------------------------------
# handle_fs_event filtering
# ---------------------------------------------------------------------------


def test_created_event_emits() -> None:
    watcher, recorder = make_watcher()
    watcher.handle_fs_event(FakeEvent("created", "/repo/new_file.py"))
    assert recorder.count == 1


def test_modified_event_emits() -> None:
    watcher, recorder = make_watcher()
    watcher.handle_fs_event(FakeEvent("modified", "/repo/src/app.py"))
    assert recorder.count == 1


def test_opened_event_is_filtered() -> None:
    # Read-only traffic (grep, nvim opening a buffer) must not fork git.
    watcher, recorder = make_watcher()
    watcher.handle_fs_event(FakeEvent("opened", "/repo/src/app.py"))
    assert recorder.count == 0


def test_closed_no_write_event_is_filtered() -> None:
    watcher, recorder = make_watcher()
    watcher.handle_fs_event(FakeEvent("closed_no_write", "/repo/src/app.py"))
    assert recorder.count == 0


def test_git_internal_event_is_filtered() -> None:
    # Without this, every scan (which refreshes .git/index mtime) would
    # schedule the next scan, forever.
    watcher, recorder = make_watcher()
    watcher.handle_fs_event(FakeEvent("modified", "/repo/.git/index"))
    assert recorder.count == 0


def test_ignored_path_event_is_filtered() -> None:
    watcher, recorder = make_watcher(ignored={"/repo/node_modules/x.js"})
    watcher.handle_fs_event(FakeEvent("created", "/repo/node_modules/x.js"))
    assert recorder.count == 0


def test_move_with_relevant_dest_emits() -> None:
    # mv out of an ignored dir into the tracked tree: src is ignored but
    # dest can change status — must emit.
    watcher, recorder = make_watcher(ignored={"/repo/build/out.txt"})
    watcher.handle_fs_event(
        FakeEvent("moved", "/repo/build/out.txt", dest_path="/repo/src/out.txt")
    )
    assert recorder.count == 1


def test_move_entirely_inside_ignored_tree_is_filtered() -> None:
    watcher, recorder = make_watcher(ignored={"/repo/build/a.txt", "/repo/build/b.txt"})
    watcher.handle_fs_event(
        FakeEvent("moved", "/repo/build/a.txt", dest_path="/repo/build/b.txt")
    )
    assert recorder.count == 0


def test_burst_is_throttled_to_one_emit() -> None:
    watcher, recorder = make_watcher()
    for i in range(20):
        watcher.handle_fs_event(FakeEvent("modified", f"/repo/f{i}.py"))
    assert recorder.count == 1


def test_emits_resume_after_throttle_window() -> None:
    watcher, recorder = make_watcher()
    watcher.handle_fs_event(FakeEvent("modified", "/repo/a.py"))
    fresh_throttle(watcher)
    watcher.handle_fs_event(FakeEvent("modified", "/repo/b.py"))
    assert recorder.count == 2


# ---------------------------------------------------------------------------
# GitController.is_path_ignored
# ---------------------------------------------------------------------------


@pytest.fixture
def controller():
    c = GitController()
    yield c
    c.stop()


def seed_ignored(c: GitController, root: str, ignored: dict[str, bool]) -> None:
    """Install a resolved root + ignored set without running a real scan."""
    with c._lock:
        c._resolved_root = root
        c._ignored_set = ignored


def test_ignored_exact_file_match(controller: GitController) -> None:
    seed_ignored(controller, "/repo", {"/repo/secret.env": True})
    assert controller.is_path_ignored("/repo/secret.env")


def test_ignored_via_directory_aggregate(controller: GitController) -> None:
    # `git ls-files --directory` lists `node_modules` once; children must
    # resolve through the ancestor walk.
    seed_ignored(controller, "/repo", {"/repo/node_modules": True})
    assert controller.is_path_ignored("/repo/node_modules/pkg/deep/file.js")


def test_clean_path_is_not_ignored(controller: GitController) -> None:
    seed_ignored(controller, "/repo", {"/repo/node_modules": True})
    assert not controller.is_path_ignored("/repo/src/main.py")


def test_repo_root_itself_is_never_ignored(controller: GitController) -> None:
    # The walk stops BEFORE the root: a stray root entry in the set must
    # not blanket-ignore the whole tree.
    seed_ignored(controller, "/repo", {"/repo": True})
    assert not controller.is_path_ignored("/repo/src/main.py")


def test_no_resolved_root_treats_nothing_as_ignored(
    controller: GitController,
) -> None:
    assert not controller.is_path_ignored("/repo/node_modules/x.js")


def test_path_outside_root_is_not_ignored(controller: GitController) -> None:
    seed_ignored(controller, "/repo", {"/repo/node_modules": True})
    assert not controller.is_path_ignored("/elsewhere/node_modules/x.js")


# ---------------------------------------------------------------------------
# GitController trigger surface
# ---------------------------------------------------------------------------


def test_poke_starts_debounce(controller: GitController) -> None:
    assert not controller._debounce.isActive()
    controller.poke()
    assert controller._debounce.isActive()


def test_worktree_changed_slot_starts_debounce(controller: GitController) -> None:
    assert not controller._debounce.isActive()
    controller._on_worktree_changed()
    assert controller._debounce.isActive()


def test_controller_owns_a_worktree_watcher(controller: GitController) -> None:
    # The watcher must exist and be wired before any scan happens — the
    # connect in __init__ is what makes `_refresh_watcher_for_root`'s
    # `set_root` call effective.
    assert isinstance(controller._worktree_watcher, WorktreeWatcher)


# ---------------------------------------------------------------------------
# Real-observer integration (inotify end-to-end)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not WATCHDOG_AVAILABLE, reason="watchdog not installed")
def test_observer_emits_on_file_creation(tmp_path) -> None:
    watcher = WorktreeWatcher(lambda p: False)
    recorder = EmitRecorder()
    # EXPLICIT DirectConnection: the emit comes from the observer thread,
    # and Qt's AutoConnection would queue it to the main thread's event
    # loop — which pytest never spins, so the signal would silently never
    # arrive. Direct runs the slot inline on the emitter thread;
    # EmitRecorder is thread-safe via its Event.
    watcher.changed.connect(recorder.on_changed, Qt.ConnectionType.DirectConnection)
    watcher.set_root(str(tmp_path))
    try:
        (tmp_path / "newfile.txt").write_text("hello")
        assert recorder.fired.wait(timeout=5.0), (
            "observer did not report the file creation"
        )
    finally:
        watcher.stop()


@pytest.mark.skipif(not WATCHDOG_AVAILABLE, reason="watchdog not installed")
def test_set_root_is_idempotent_and_stoppable(tmp_path) -> None:
    watcher = WorktreeWatcher(lambda p: False)
    watcher.set_root(str(tmp_path))
    first = watcher._observer
    watcher.set_root(str(tmp_path))  # same root — must not rebuild
    assert watcher._observer is first
    watcher.set_root("")  # teardown path
    assert watcher._observer is None
    watcher.stop()  # double-stop must be safe


@pytest.mark.skipif(not WATCHDOG_AVAILABLE, reason="watchdog not installed")
def test_modification_in_place_emits(tmp_path) -> None:
    # The decisive case QFileSystemWatcher could NOT deliver: an in-place
    # write (agent Edit, `echo >>`) with no create/rename in the parent.
    target = tmp_path / "tracked.py"
    target.write_text("v1")
    watcher = WorktreeWatcher(lambda p: False)
    recorder = EmitRecorder()
    # DirectConnection — see test_observer_emits_on_file_creation.
    watcher.changed.connect(recorder.on_changed, Qt.ConnectionType.DirectConnection)
    watcher.set_root(str(tmp_path))
    try:
        # Give the emitter thread a beat to arm the inotify watches.
        time.sleep(0.2)
        with target.open("a") as f:
            f.write("appended")
        assert recorder.fired.wait(timeout=5.0), (
            "observer did not report the in-place modification"
        )
    finally:
        watcher.stop()
