"""Tests for the per-project expanded-state cache.

Covers the contract `tree_state_cache.py` exposes to `AppController`:
- missing cache file → `[]`
- save → load round trip preserves the set (order is sorted, dupes coalesced)
- corrupted JSON / wrong shape / unknown schema version → `[]`
- non-existent paths are pruned at load (stat'd against disk)
- atomic write: no temp file leaks after a successful save
- empty paths input writes a cache file (not deletion)
- `XDG_STATE_HOME` is honoured

The module's atomic-write guarantee against mid-save crashes is hard
to unit-test deterministically (we'd need a fault-injection layer).
We assert the *observable* invariants — temp-file cleanup on success,
fault-tolerant load — and trust `os.replace`'s POSIX guarantees for
the rest.
"""

from __future__ import annotations

import json

import pytest

from symmetria_ide import tree_state_cache as tsc
from symmetria_ide.state_paths import repo_hash


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Redirect the cache state dir to a per-test tmp path."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    # The module memoizes nothing — every call resolves the env fresh.
    return tmp_path / "symmetria-ide" / "projects"


def test_load_missing_returns_empty(state_dir, tmp_path):
    """No cache file → `[]`."""
    repo = tmp_path / "fresh-repo"
    repo.mkdir()
    assert tsc.load_expanded(str(repo)) == []


def test_save_then_load_round_trip(state_dir, tmp_path):
    """Saved paths come back as a sorted list of strings."""
    repo = tmp_path / "repo"
    repo.mkdir()
    a = repo / "a"
    b = repo / "b"
    c = repo / "c"
    for d in (a, b, c):
        d.mkdir()
    tsc.save_expanded(str(repo), [str(c), str(a), str(b)])
    out = tsc.load_expanded(str(repo))
    assert out == sorted([str(a), str(b), str(c)])


def test_save_coalesces_duplicates(state_dir, tmp_path):
    """Duplicate inputs collapse — `set` semantics under the hood."""
    repo = tmp_path / "repo"
    repo.mkdir()
    a = repo / "a"
    a.mkdir()
    tsc.save_expanded(str(repo), [str(a), str(a), str(a)])
    assert tsc.load_expanded(str(repo)) == [str(a)]


def test_load_prunes_missing_paths(state_dir, tmp_path):
    """Paths that no longer exist on disk are dropped at load."""
    repo = tmp_path / "repo"
    repo.mkdir()
    live = repo / "live"
    gone = repo / "gone"
    live.mkdir()
    gone.mkdir()
    tsc.save_expanded(str(repo), [str(live), str(gone)])
    # User deletes `gone` between sessions.
    gone.rmdir()
    out = tsc.load_expanded(str(repo))
    assert out == [str(live)]
    assert str(gone) not in out


def test_load_drops_file_paths(state_dir, tmp_path):
    """A path that exists as a regular file (not dir) is pruned.

    A renamed-dir-to-file or a stale cache pointing at a now-file
    should not be restored. `os.path.isdir` is the right check.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    weirdo = repo / "weirdo"
    weirdo.mkdir()
    tsc.save_expanded(str(repo), [str(weirdo)])
    weirdo.rmdir()
    weirdo.touch()  # now a file, not a dir
    assert tsc.load_expanded(str(repo)) == []


def test_load_handles_corrupted_json(state_dir, tmp_path):
    """Garbage in the cache file → `[]`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    tsc.save_expanded(str(repo), [])
    target = tsc._cache_path(str(repo))
    target.write_text("not valid json {", encoding="utf-8")
    assert tsc.load_expanded(str(repo)) == []


def test_load_rejects_unknown_schema_version(state_dir, tmp_path):
    """Out-of-range schema version → `[]` (downgrade and invalid-version safety).

    Covers both the forward case (version > KNOWN_VERSION) and the
    backward/invalid case (version < 1, e.g. 0 from a truncated write).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    target = tsc._cache_path(str(repo))
    target.parent.mkdir(parents=True, exist_ok=True)

    # Forward version (future writer, downgraded IDE).
    target.write_text(
        json.dumps(
            {
                "version": tsc.KNOWN_VERSION + 99,
                "repo_root": str(repo),
                "expanded_paths": [str(repo)],
            }
        ),
        encoding="utf-8",
    )
    assert tsc.load_expanded(str(repo)) == []

    # Version 0 — could result from a truncated or corrupt write.
    target.write_text(
        json.dumps(
            {
                "version": 0,
                "repo_root": str(repo),
                "expanded_paths": [str(repo)],
            }
        ),
        encoding="utf-8",
    )
    assert tsc.load_expanded(str(repo)) == []


def test_load_rejects_wrong_shape(state_dir, tmp_path):
    """Top-level non-dict or missing `expanded_paths` → `[]`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    target = tsc._cache_path(str(repo))
    target.parent.mkdir(parents=True, exist_ok=True)

    # Top-level is a list, not a dict.
    target.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert tsc.load_expanded(str(repo)) == []

    # Top-level dict but `expanded_paths` is the wrong type.
    target.write_text(
        json.dumps({"version": 1, "expanded_paths": "string-not-list"}),
        encoding="utf-8",
    )
    assert tsc.load_expanded(str(repo)) == []


def test_save_empty_writes_file(state_dir, tmp_path):
    """`save_expanded(root, [])` writes the file — does NOT delete it.

    The empty-list state is semantically distinct from "no cache" —
    it means "the user has opened this project but expanded nothing".
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    tsc.save_expanded(str(repo), [])
    target = tsc._cache_path(str(repo))
    assert target.exists()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["expanded_paths"] == []
    assert data["version"] == tsc.KNOWN_VERSION


def test_save_does_not_leak_tempfile(state_dir, tmp_path):
    """After a successful save, no `.tree-state-*.tmp` artifacts remain."""
    repo = tmp_path / "repo"
    repo.mkdir()
    a = repo / "a"
    a.mkdir()
    tsc.save_expanded(str(repo), [str(a)])
    leftovers = [
        p
        for p in state_dir.iterdir()
        if p.name.startswith(".tree-state-") and p.name.endswith(".json.tmp")
    ]
    assert leftovers == []


def test_repo_hash_is_stable_and_path_sensitive(tmp_path):
    """Same path → same hash; different paths → different hashes.

    The 16-char truncation is large enough that any plausible per-user
    project set won't see collisions, but verify the basic contract.
    """
    h1 = repo_hash("/home/jc/projects/a")
    h2 = repo_hash("/home/jc/projects/a")
    h3 = repo_hash("/home/jc/projects/b")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 16


def test_xdg_state_home_is_honoured(tmp_path, monkeypatch):
    """The state dir respects `$XDG_STATE_HOME` and creates `projects/`."""
    custom = tmp_path / "custom-xdg"
    monkeypatch.setenv("XDG_STATE_HOME", str(custom))
    repo = tmp_path / "repo"
    repo.mkdir()
    tsc.save_expanded(str(repo), [])
    expected = custom / "symmetria-ide" / "projects"
    assert expected.exists()
    assert (expected / f"{repo_hash(str(repo))}.json").exists()


def test_empty_repo_root_is_noop(state_dir):
    """Defensive: empty `repo_root` neither reads nor writes."""
    assert tsc.load_expanded("") == []
    tsc.save_expanded("", ["/whatever"])  # must not raise


def test_save_overwrites_previous(state_dir, tmp_path):
    """A second save replaces the first — no append semantics."""
    repo = tmp_path / "repo"
    repo.mkdir()
    a = repo / "a"
    b = repo / "b"
    a.mkdir()
    b.mkdir()
    tsc.save_expanded(str(repo), [str(a)])
    tsc.save_expanded(str(repo), [str(b)])
    out = tsc.load_expanded(str(repo))
    assert out == [str(b)]


def test_non_string_paths_in_save_are_filtered(state_dir, tmp_path):
    """Non-string entries in the input list are silently dropped.

    Defends against a future caller passing in QString-like objects
    that have already been converted, or accidental None entries.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    a = repo / "a"
    a.mkdir()
    # mypy would catch this in static analysis; the runtime guard
    # exists for hostile / accidental inputs.
    tsc.save_expanded(str(repo), [str(a), None, 42, str(a)])  # type: ignore[list-item]
    assert tsc.load_expanded(str(repo)) == [str(a)]
