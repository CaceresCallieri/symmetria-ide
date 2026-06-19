"""Tests for the shared atomic JSON write helper.

`atomic_write_json` is the write path behind both the tree-state cache and
the per-project browser marker. We assert the observable invariants —
round-trip fidelity, parent-dir creation, temp-file cleanup on success,
and graceful failure (False, no raise) on an unwritable destination. The
mid-write crash safety itself rests on `os.replace`'s POSIX atomicity and
isn't unit-testable without a fault-injection layer.
"""

from __future__ import annotations

import json

from symmetria_ide.fs_atomic import atomic_write_json


def test_round_trip(tmp_path):
    """Payload writes and reads back byte-faithfully (pretty + trailing nl)."""
    target = tmp_path / "out.json"
    payload = {"version": 1, "browser_agents": True, "list": [3, 1, 2]}
    assert atomic_write_json(target, payload) is True
    assert json.loads(target.read_text(encoding="utf-8")) == payload
    # Pretty-printed with a trailing newline (stable on-disk representation).
    assert target.read_text(encoding="utf-8").endswith("}\n")


def test_creates_missing_parent_dirs(tmp_path):
    """A nested target whose parent dirs don't exist is created on write.

    This is what lets the marker writer drop a file into a fresh
    `.symmetria/` without an explicit mkdir.
    """
    target = tmp_path / "nested" / "deeper" / "ide.json"
    assert atomic_write_json(target, {"ok": True}) is True
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}


def test_overwrites_existing(tmp_path):
    """A second write fully replaces the first (rename-over, not append)."""
    target = tmp_path / "out.json"
    atomic_write_json(target, {"v": 1})
    atomic_write_json(target, {"v": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 2}


def test_no_tempfile_leak_on_success(tmp_path):
    """After a successful write, no `.<name>-*.tmp` scratch file remains."""
    target = tmp_path / "out.json"
    atomic_write_json(target, {"ok": True})
    leftovers = [
        p
        for p in tmp_path.iterdir()
        if p.name.startswith(f".{target.name}-") and p.name.endswith(".tmp")
    ]
    assert leftovers == []


def test_unwritable_target_returns_false(tmp_path):
    """An undirectory-able destination → False, no raise (caller decides).

    Pointing the target's "parent" at an existing regular file makes the
    parent mkdir / tempfile creation raise OSError internally.
    """
    blocker = tmp_path / "iam_a_file"
    blocker.write_text("not a dir", encoding="utf-8")
    target = blocker / "child.json"  # parent is a file → mkdir/mkstemp fails
    assert atomic_write_json(target, {"ok": True}) is False
