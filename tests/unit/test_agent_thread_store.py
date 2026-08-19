"""Phase-3 specs for durable per-project agent-thread metadata.

The harness remains the source of transcript history.  This store owns only
the work root that the IDE observes while an agent is live, keyed by durable
``(harness, session_id)`` identity.
"""

from __future__ import annotations

import importlib
import multiprocessing
import time


def _store():
    return importlib.import_module("symmetria_ide.agent_thread_store")


def test_work_root_round_trips_per_project_and_durable_thread_identity(
    tmp_path, monkeypatch
) -> None:
    """Spec: work roots survive restart without cross-project/harness bleed."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store = _store()
    project_a = str(tmp_path / "project-a")
    project_b = str(tmp_path / "project-b")

    assert store.save_work_root(project_a, "claude", "same-id", "/work/a")
    assert store.save_work_root(project_a, "opencode", "same-id", "/work/o")
    assert store.save_work_root(project_b, "claude", "same-id", "/work/b")

    assert store.load_work_root(project_a, "claude", "same-id") == "/work/a"
    assert store.load_work_root(project_a, "opencode", "same-id") == "/work/o"
    assert store.load_work_root(project_b, "claude", "same-id") == "/work/b"
    assert store.load_work_root(project_a, "claude", "missing") is None


def test_work_root_store_uses_versioned_atomic_json(monkeypatch, tmp_path) -> None:
    """Spec: metadata updates use the shared crash-safe JSON write seam."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store = _store()
    writes: list[tuple[object, dict]] = []
    monkeypatch.setattr(
        store,
        "atomic_write_json",
        lambda path, payload: writes.append((path, payload)) or True,
    )

    assert store.save_work_root("/project", "claude", "session-1", "/worktree")

    assert len(writes) == 1
    _path, payload = writes[0]
    assert payload["version"] == store.KNOWN_VERSION
    entry = payload["threads"]["claude:session-1"]
    assert entry["work_root"] == "/worktree"
    assert set(entry) == {"work_root", "updated_at"}
    assert isinstance(entry["updated_at"], int)


def test_concurrent_project_writers_do_not_lose_thread_entries(
    monkeypatch, tmp_path
) -> None:
    """Regression: sibling IDE windows serialize the shared read/merge/write."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store = _store()
    project = str(tmp_path / "project")
    worker_count = 8

    # Widen the read-before-write race deterministically. With the project lock,
    # workers take this delay one at a time and each sees the previous merge;
    # without it they all read the same stale document before any writes land.
    original_load = store._load_document

    def slow_load(project_root: str) -> dict:
        document = original_load(project_root)
        time.sleep(0.03)
        return document

    monkeypatch.setattr(store, "_load_document", slow_load)
    context = multiprocessing.get_context("fork")
    start = context.Event()

    def save(index: int) -> None:
        start.wait()
        store.save_work_root(
            project,
            "claude",
            f"session-{index}",
            f"/worktree/{index}",
        )

    workers = [
        context.Process(target=save, args=(index,)) for index in range(worker_count)
    ]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(timeout=10)

    assert [worker.exitcode for worker in workers] == [0] * worker_count
    assert {
        key: entry["work_root"] for key, entry in store.load_threads(project).items()
    } == {
        f"claude:session-{index}": f"/worktree/{index}" for index in range(worker_count)
    }
