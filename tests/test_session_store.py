"""Tests for the per-project saved-session manifest store.

Covers the contract `session_store.py` exposes to `AppController`:
- missing session file → `load` returns None, `exists` False
- save → load round trip preserves the manifest body + stamps version/root/saved_at
- corrupted JSON / non-dict top level / out-of-range schema version → None
- `exists` reflects *restorability* (rejects corrupt/forward-version files)
- `delete` removes the file and is idempotent
- empty root is a defensive no-op
- `$XDG_STATE_HOME` is honoured, sessions land under `sessions/`

Mirrors `test_tree_state_cache.py`'s style: the atomic-write crash guarantee
is trusted to `os.replace`; we assert the observable invariants only.
"""

from __future__ import annotations

import json

import pytest

from symmetria_ide import session_store as ss
from symmetria_ide.state_paths import repo_hash


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    """Redirect the state dir to a per-test tmp path; return the sessions dir."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path / "symmetria-ide" / "sessions"


def _manifest() -> dict:
    """A representative non-trivial manifest body (no version/root/saved_at —
    those are stamped by `save`)."""
    return {
        "anchored": True,
        "central_surface": "agent",
        "focused_agent": 2,
        "focused_browser": 0,
        "agents": [
            {
                "slot": 1,
                "display_order": 1,
                "harness": "claude",
                "spawn_type": "resume",
                "session_id": "abc-123",
                "dangerous": True,
                "title": "fix the bug",
            },
            {
                "slot": 2,
                "display_order": 2,
                "harness": "opencode",
                "spawn_type": "resume",
                "session_id": "ses_xyz",
                "dangerous": False,
                "title": "",
            },
        ],
        "browsers": [{"order": 1, "url": "https://example.com"}],
        "editor": {
            "active": "/abs/file.py",
            "line": 42,
            "col": 3,
            "files": ["/abs/a.py", "/abs/file.py"],
        },
    }


def test_load_missing_returns_none(sessions_dir):
    """No session file → None."""
    assert ss.load("/home/jc/projects/never-saved") is None


def test_exists_missing_is_false(sessions_dir):
    assert ss.exists("/home/jc/projects/never-saved") is False


def test_save_then_load_round_trip(sessions_dir):
    """The manifest body survives a save→load round trip verbatim, with the
    provenance fields stamped on."""
    root = "/home/jc/projects/demo"
    assert ss.save(root, _manifest()) is True

    out = ss.load(root)
    assert out is not None
    assert out["version"] == ss.KNOWN_VERSION
    assert out["root"] == root
    assert "saved_at" in out
    # Body preserved exactly.
    assert out["agents"] == _manifest()["agents"]
    assert out["browsers"] == _manifest()["browsers"]
    assert out["editor"] == _manifest()["editor"]
    assert out["central_surface"] == "agent"
    assert out["focused_agent"] == 2
    assert out["anchored"] is True


def test_exists_true_after_save(sessions_dir):
    root = "/home/jc/projects/demo"
    ss.save(root, _manifest())
    assert ss.exists(root) is True


def test_save_stamps_override_caller_values(sessions_dir):
    """Caller-supplied version/root/saved_at are overwritten so they can't
    drift from the actual key/schema."""
    root = "/home/jc/projects/demo"
    bogus = _manifest()
    bogus["version"] = 999
    bogus["root"] = "/somewhere/else"
    bogus["saved_at"] = "1999-01-01T00:00:00Z"
    ss.save(root, bogus)
    out = ss.load(root)
    assert out["version"] == ss.KNOWN_VERSION
    assert out["root"] == root
    assert out["saved_at"] != "1999-01-01T00:00:00Z"


def test_save_overwrites_previous(sessions_dir):
    """A second save replaces the first — no merge."""
    root = "/home/jc/projects/demo"
    ss.save(root, {"central_surface": "editor", "agents": []})
    ss.save(root, {"central_surface": "terminal", "agents": []})
    out = ss.load(root)
    assert out["central_surface"] == "terminal"


def test_load_handles_corrupted_json(sessions_dir):
    """Garbage in the session file → None."""
    root = "/home/jc/projects/demo"
    ss.save(root, _manifest())
    target = ss._session_path(root)
    target.write_text("not valid json {", encoding="utf-8")
    assert ss.load(root) is None
    assert ss.exists(root) is False


def test_load_rejects_unknown_schema_version(sessions_dir):
    """Out-of-range schema version → None (forward and < 1)."""
    root = "/home/jc/projects/demo"
    target = ss._session_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)

    target.write_text(
        json.dumps({"version": ss.KNOWN_VERSION + 99, "agents": []}),
        encoding="utf-8",
    )
    assert ss.load(root) is None
    assert ss.exists(root) is False

    target.write_text(json.dumps({"version": 0, "agents": []}), encoding="utf-8")
    assert ss.load(root) is None


def test_load_rejects_non_dict(sessions_dir):
    """Top-level non-dict → None."""
    root = "/home/jc/projects/demo"
    target = ss._session_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert ss.load(root) is None


def test_delete_removes_file(sessions_dir):
    root = "/home/jc/projects/demo"
    ss.save(root, _manifest())
    assert ss.exists(root) is True
    ss.delete(root)
    assert ss.exists(root) is False
    assert not ss._session_path(root).exists()


def test_delete_is_idempotent(sessions_dir):
    """Deleting a non-existent session must not raise."""
    ss.delete("/home/jc/projects/never-saved")  # no file yet
    ss.delete("/home/jc/projects/never-saved")  # still no file


def test_empty_root_is_noop(sessions_dir):
    """Defensive: empty root neither reads nor writes nor raises."""
    assert ss.load("") is None
    assert ss.exists("") is False
    assert ss.save("", _manifest()) is False
    ss.delete("")  # must not raise


def test_xdg_state_home_is_honoured(tmp_path, monkeypatch):
    """Sessions land under `$XDG_STATE_HOME/symmetria-ide/sessions/<hash>.json`."""
    custom = tmp_path / "custom-xdg"
    monkeypatch.setenv("XDG_STATE_HOME", str(custom))
    root = "/home/jc/projects/demo"
    ss.save(root, _manifest())
    expected = custom / "symmetria-ide" / "sessions" / f"{repo_hash(root)}.json"
    assert expected.exists()
