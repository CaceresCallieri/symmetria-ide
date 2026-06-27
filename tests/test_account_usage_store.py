"""Pure-function tests for the cross-IDE account-usage peer file.

Exercises `read` / `publish_if_newer` against a tmp file — no Qt, no watcher.
The last-write-wins-by-`observed_at_ns` contract is what keeps the shared file
convergent across IDE instances (and keeps an IDE's own write from looping its
watcher), so it's worth pinning.
"""

from __future__ import annotations

from symmetria_ide.account_usage_store import (
    AccountUsageStore,
    publish_if_newer,
    read,
)


def _usage(ts: int, five_pct: int = 10) -> dict:
    return {
        "five_pct": five_pct,
        "five_reset": 100,
        "seven_pct": 50,
        "seven_reset": 200,
        "observed_at_ns": ts,
    }


def test_read_absent_file_is_none(tmp_path):
    assert read(tmp_path / "nope.json") is None


def test_publish_writes_when_absent(tmp_path):
    p = tmp_path / "usage.json"
    assert publish_if_newer(p, _usage(1000)) is True
    got = read(p)
    assert got is not None and got["observed_at_ns"] == 1000


def test_publish_no_ops_on_older(tmp_path):
    p = tmp_path / "usage.json"
    publish_if_newer(p, _usage(1000, five_pct=20))
    assert publish_if_newer(p, _usage(500, five_pct=99)) is False
    assert read(p)["five_pct"] == 20  # unchanged


def test_publish_no_ops_on_equal_ts(tmp_path):
    # Equal ts is "not newer" — this is what makes our own write a no-op when it
    # comes back through the watcher, so there's no write loop.
    p = tmp_path / "usage.json"
    publish_if_newer(p, _usage(1000))
    assert publish_if_newer(p, _usage(1000)) is False


def test_publish_overwrites_on_newer(tmp_path):
    p = tmp_path / "usage.json"
    publish_if_newer(p, _usage(1000, five_pct=20))
    assert publish_if_newer(p, _usage(2000, five_pct=33)) is True
    assert read(p)["five_pct"] == 33


def test_publish_rejects_zero_timestamp(tmp_path):
    p = tmp_path / "usage.json"
    assert publish_if_newer(p, _usage(0)) is False
    assert read(p) is None


def test_read_tolerates_partial_json(tmp_path):
    p = tmp_path / "usage.json"
    p.write_text("{ not valid json")
    assert read(p) is None


def test_read_rejects_dict_without_observed_at(tmp_path):
    p = tmp_path / "usage.json"
    p.write_text('{"five_pct": 10}')
    assert read(p) is None


# ---------------------------------------------------------------------------
# AccountUsageStore watcher self-heal (the QFileSystemWatcher re-arm after the
# atomic rename — the most ordering-fragile part of the module). Handlers are
# invoked DIRECTLY so the assertions stay synchronous (no event-loop pumping —
# see the processevents_shared_app_segv memory). Relies on the session-scoped
# QCoreApplication fixture in conftest.
# ---------------------------------------------------------------------------


def _store(tmp_path):
    return AccountUsageStore(path=tmp_path / "usage.json")


def test_store_rearms_watch_after_publish(tmp_path):
    store = _store(tmp_path)
    store.start()  # file absent yet → only the dir is watched
    store.publish(_usage(1000))  # writes the file, then re-arms the file watch
    assert str(tmp_path / "usage.json") in store._watcher.files()
    store.stop()


def test_on_dir_changed_emits_once_on_first_appearance(tmp_path):
    store = _store(tmp_path)
    store.start()
    emits: list[int] = []
    store.changed.connect(lambda: emits.append(1))
    # A peer creates the file (we write it WITHOUT going through store.publish,
    # so the watcher hasn't been told — directoryChanged is what must catch it).
    publish_if_newer(tmp_path / "usage.json", _usage(1000))
    store._on_dir_changed(str(tmp_path))
    assert str(tmp_path / "usage.json") in store._watcher.files()  # now watched
    assert len(emits) == 1
    # Already watched → a second directoryChanged (sibling churn) is a no-op.
    store._on_dir_changed(str(tmp_path))
    assert len(emits) == 1
    store.stop()


def test_on_file_changed_rearms_after_rename_drop(tmp_path):
    store = _store(tmp_path)
    store.start()
    store.publish(_usage(1000))
    path = str(tmp_path / "usage.json")
    # Simulate the atomic-rename drop: QFileSystemWatcher loses the path.
    store._watcher.removePath(path)
    assert path not in store._watcher.files()
    emits: list[int] = []
    store.changed.connect(lambda: emits.append(1))
    store._on_file_changed(path)
    assert path in store._watcher.files()  # re-armed
    assert len(emits) == 1
    store.stop()
