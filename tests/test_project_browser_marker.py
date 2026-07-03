"""Tests for the per-project agent-browser opt-in marker.

Covers the contract `AppController` relies on:
- default OFF when no marker exists
- set → read round trip (enable and disable)
- the marker is anchored at the repo root (walk-up from a subdir)
- `.git` as a dir AND as a file (worktree/submodule) both anchor the root
- `.symmetria/` is created on write
- fault tolerance: malformed / wrong-shape / non-`true` values → False
- blank input is a no-op
- the written file is the committable shape (version + browser_agents)
"""

from __future__ import annotations

import json

from symmetria_ide import project_browser_marker as pbm


def test_default_off_without_marker(tmp_path):
    """A project with no marker (and no git) → disabled."""
    repo = tmp_path / "plain"
    repo.mkdir()
    assert pbm.browser_agents_enabled(str(repo)) is False


def test_enable_then_read(tmp_path):
    """set(True) → enabled; the file lands under <root>/.symmetria/."""
    repo = tmp_path / "web-app"
    repo.mkdir()
    root = pbm.set_browser_agents(str(repo), True)
    assert root == str(repo)
    assert pbm.browser_agents_enabled(str(repo)) is True
    assert (repo / ".symmetria" / "ide.json").exists()


def test_disable_round_trip(tmp_path):
    """set(False) after set(True) → disabled again."""
    repo = tmp_path / "web-app"
    repo.mkdir()
    pbm.set_browser_agents(str(repo), True)
    assert pbm.browser_agents_enabled(str(repo)) is True
    pbm.set_browser_agents(str(repo), False)
    assert pbm.browser_agents_enabled(str(repo)) is False


def test_marker_anchors_at_git_root_from_subdir(tmp_path):
    """Walk-up: a marker written from a deep subdir lands at the .git root,
    and is then visible from that subdir."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)  # repo is the git root
    deep = repo / "src" / "feature"
    deep.mkdir(parents=True)

    root = pbm.set_browser_agents(str(deep), True)
    assert root == str(repo)
    # Marker at the repo root, NOT at the subdir.
    assert (repo / ".symmetria" / "ide.json").exists()
    assert not (deep / ".symmetria").exists()
    # Readable from the subdir (resolves the same root).
    assert pbm.browser_agents_enabled(str(deep)) is True


def test_resolve_root_handles_git_file(tmp_path):
    """A `.git` FILE (worktree/submodule), not a dir, still anchors the root."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /somewhere/.git/worktrees/wt\n")
    deep = wt / "nested"
    deep.mkdir()
    assert pbm.resolve_project_root(str(deep)) == str(wt)


def test_resolve_root_finds_existing_symmetria_dir(tmp_path):
    """An existing `.symmetria/` (committed marker) anchors the root even
    without a `.git`."""
    proj = tmp_path / "proj"
    (proj / ".symmetria").mkdir(parents=True)
    deep = proj / "a" / "b"
    deep.mkdir(parents=True)
    assert pbm.resolve_project_root(str(deep)) == str(proj)


def test_resolve_root_falls_back_to_start(tmp_path):
    """No `.git` / `.symmetria` anywhere up the tree → return the start dir."""
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    assert pbm.resolve_project_root(str(lonely)) == str(lonely)


def test_malformed_marker_is_disabled(tmp_path):
    """Garbage JSON in the marker → disabled (fault-tolerant)."""
    repo = tmp_path / "repo"
    (repo / ".symmetria").mkdir(parents=True)
    (repo / ".symmetria" / "ide.json").write_text("not json {", encoding="utf-8")
    assert pbm.browser_agents_enabled(str(repo)) is False


def test_wrong_shape_or_value_is_disabled(tmp_path):
    """Only an explicit `browser_agents: true` enables — everything else off."""
    repo = tmp_path / "repo"
    marker = repo / ".symmetria" / "ide.json"
    marker.parent.mkdir(parents=True)

    for payload in (
        ["a", "list"],  # non-dict top level
        {"version": 1},  # missing field
        {"browser_agents": False},  # explicitly off
        {"browser_agents": "true"},  # string, not bool → not `is True`
        {"browser_agents": 1},  # truthy int, but not `is True`
    ):
        marker.write_text(json.dumps(payload), encoding="utf-8")
        assert pbm.browser_agents_enabled(str(repo)) is False


def test_blank_input_is_noop(tmp_path):
    """Empty start → no read, no write, no raise."""
    assert pbm.resolve_project_root("") == ""
    assert pbm.browser_agents_enabled("") is False
    assert pbm.set_browser_agents("", True) == ""


def test_written_marker_is_committable_shape(tmp_path):
    """The written file carries the version + the boolean — the shape a
    teammate's checkout reads."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pbm.set_browser_agents(str(repo), True)
    data = json.loads((repo / ".symmetria" / "ide.json").read_text(encoding="utf-8"))
    assert data == {"version": pbm.MARKER_VERSION, "browser_agents": True}


# ---------------------------------------------------------------------------
# harness_model_effort — per-project model/effort launch defaults
# ---------------------------------------------------------------------------


def _write_marker(repo, payload):
    marker = repo / ".symmetria" / "ide.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(payload), encoding="utf-8")


def test_harness_defaults_absent_returns_empty(tmp_path):
    """No marker / no harness_defaults block → ('', '') for any harness."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert pbm.harness_model_effort(str(repo), "claude") == ("", "")
    _write_marker(repo, {"version": 1, "browser_agents": True})
    assert pbm.harness_model_effort(str(repo), "claude") == ("", "")


def test_harness_defaults_read_model_and_effort(tmp_path):
    repo = tmp_path / "repo"
    _write_marker(
        repo,
        {
            "version": 1,
            "harness_defaults": {
                "claude": {"model": "opus", "effort": "high"},
                "opencode": {"model": "anthropic/x", "effort": "medium"},
            },
        },
    )
    assert pbm.harness_model_effort(str(repo), "claude") == ("opus", "high")
    assert pbm.harness_model_effort(str(repo), "opencode") == ("anthropic/x", "medium")


def test_harness_defaults_unknown_harness_is_empty(tmp_path):
    repo = tmp_path / "repo"
    _write_marker(
        repo, {"harness_defaults": {"claude": {"model": "opus", "effort": "high"}}}
    )
    assert pbm.harness_model_effort(str(repo), "opencode") == ("", "")


def test_harness_defaults_partial_entry(tmp_path):
    """Only one axis committed → the other is ''."""
    repo = tmp_path / "repo"
    _write_marker(repo, {"harness_defaults": {"claude": {"model": "sonnet"}}})
    assert pbm.harness_model_effort(str(repo), "claude") == ("sonnet", "")


def test_harness_defaults_wrong_types_are_empty(tmp_path):
    """Non-string / non-dict shapes are all fault-tolerantly dropped."""
    repo = tmp_path / "repo"
    for block in (
        {"harness_defaults": "not-a-dict"},
        {"harness_defaults": {"claude": "not-a-dict"}},
        {"harness_defaults": {"claude": {"model": 5, "effort": ["high"]}}},
        {"harness_defaults": {"claude": {"model": "", "effort": ""}}},
    ):
        _write_marker(repo, block)
        assert pbm.harness_model_effort(str(repo), "claude") == ("", "")


def test_harness_defaults_survive_malformed_marker(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".symmetria").mkdir(parents=True)
    (repo / ".symmetria" / "ide.json").write_text("not json {", encoding="utf-8")
    assert pbm.harness_model_effort(str(repo), "claude") == ("", "")


# ---------------------------------------------------------------------------
# set_browser_agents — read-merge-write must preserve harness_defaults
# ---------------------------------------------------------------------------


def test_browser_toggle_preserves_harness_defaults(tmp_path):
    """The regression guard: toggling browser via the popup must NOT wipe a
    committed harness_defaults block (a bare overwrite would)."""
    repo = tmp_path / "repo"
    _write_marker(
        repo,
        {
            "version": 1,
            "browser_agents": False,
            "harness_defaults": {"claude": {"model": "opus", "effort": "high"}},
        },
    )
    pbm.set_browser_agents(str(repo), True)
    data = json.loads((repo / ".symmetria" / "ide.json").read_text(encoding="utf-8"))
    assert data["browser_agents"] is True
    assert data["harness_defaults"] == {"claude": {"model": "opus", "effort": "high"}}
    # And the model/effort reader still sees them after the toggle.
    assert pbm.harness_model_effort(str(repo), "claude") == ("opus", "high")


def test_browser_toggle_preserves_unknown_future_fields(tmp_path):
    """Forward-compat: an unknown field a newer IDE wrote survives an older
    toggle path too (merge, not overwrite)."""
    repo = tmp_path / "repo"
    _write_marker(repo, {"version": 1, "some_future_field": {"a": 1}})
    pbm.set_browser_agents(str(repo), True)
    data = json.loads((repo / ".symmetria" / "ide.json").read_text(encoding="utf-8"))
    assert data["some_future_field"] == {"a": 1}
    assert data["browser_agents"] is True
