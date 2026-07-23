"""Tests for the MULTI-ROOT per-agent change filter (v2).

Covers the three foreign-repo pieces added on top of the single-repo v1/v2
filter: `probe_status_map` (the parsed-map twin of `probe_dirty_leaves`),
`_partition_foreign_touched` (grouping an agent's touched paths by repo,
excluding the displayed one), and the AppController projection that turns a
focused agent's foreign changes into collapsible section descriptors +
cross-repo totals + a per-repo status lookup.

Real git repos under `tmp_path`; the async pool → queued-signal hop is standard
Qt (covered elsewhere) so foreign probe results are hand-delivered via
`_on_foreign_probe_ready`, per the project's no-processEvents-in-tests rule.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from symmetria_ide.agent_bash_attribution import probe_status_map
from symmetria_ide.app import AppController, _partition_foreign_touched
from symmetria_ide.git_controller import parse_porcelain_v2


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", "-C", root, *args], check=True, capture_output=True)


def _init_repo(path: Path) -> str:
    """A realpath'd git repo with one committed file, clean working tree."""
    path.mkdir(parents=True, exist_ok=True)
    root = os.path.realpath(str(path))
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    (Path(root) / "committed.py").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


# --- probe_status_map --------------------------------------------------------


def test_probe_status_map_returns_chars(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    (Path(root) / "committed.py").write_text("changed\n")  # modified (tracked)
    (Path(root) / "new.py").write_text("new\n")  # untracked
    m = probe_status_map(root)
    assert m is not None
    assert m["committed.py"].char == "M"
    assert m["new.py"].char == "?"


def test_probe_status_map_clean_is_empty(tmp_path: Path) -> None:
    root = _init_repo(tmp_path)
    assert probe_status_map(root) == {}


def test_probe_status_map_non_repo_and_blank_are_none(tmp_path: Path) -> None:
    # A FAILURE is None (distinct from a clean repo's empty map), so the caller
    # keeps any prior snapshot rather than wiping a section spuriously.
    assert probe_status_map(os.path.realpath(str(tmp_path))) is None
    assert probe_status_map("") is None


# --- _partition_foreign_touched ---------------------------------------------


def test_partition_excludes_displayed_groups_foreign(tmp_path: Path) -> None:
    displayed = _init_repo(tmp_path / "displayed")
    foreign = _init_repo(tmp_path / "foreign")
    touched = {
        os.path.join(displayed, "a.py"),
        os.path.join(displayed, "b.py"),
        os.path.join(foreign, "c.py"),
    }
    groups = _partition_foreign_touched(touched, displayed)
    # The displayed repo's two paths are dropped; only the foreign repo groups.
    assert set(groups) == {foreign}
    assert groups[foreign] == {os.path.join(foreign, "c.py")}


def test_partition_drops_paths_under_no_repo(tmp_path: Path) -> None:
    displayed = _init_repo(tmp_path / "displayed")
    loose = os.path.realpath(str(tmp_path / "loose.txt"))  # under no git repo
    groups = _partition_foreign_touched({loose}, displayed)
    assert groups == {}


def test_partition_two_foreign_repos(tmp_path: Path) -> None:
    displayed = _init_repo(tmp_path / "displayed")
    fa = _init_repo(tmp_path / "fa")
    fb = _init_repo(tmp_path / "fb")
    groups = _partition_foreign_touched(
        {os.path.join(fa, "x"), os.path.join(fb, "y"), os.path.join(fb, "z")},
        displayed,
    )
    assert set(groups) == {fa, fb}
    assert groups[fb] == {os.path.join(fb, "y"), os.path.join(fb, "z")}


# --- AppController projection ------------------------------------------------


@pytest.fixture
def controller():
    return AppController()


def _feed_displayed_status(controller: AppController, root: str) -> None:
    blob = subprocess.run(
        ["git", "-C", root, "status", "--porcelain=v2", "-z"], capture_output=True
    ).stdout
    gc = controller._git_controller
    with gc._lock:
        gc._status_map = parse_porcelain_v2(blob)
        gc._resolved_root = root


def test_foreign_section_appears_with_count_and_total(controller, tmp_path) -> None:
    displayed = _init_repo(tmp_path / "displayed")
    foreign = _init_repo(tmp_path / "foreign")
    controller._cwd = displayed  # → displayedRoot (no override/anchor)

    # The agent edited one file in the displayed repo and one in the foreign.
    (Path(displayed) / "edited.py").write_text("agent\n")
    (Path(foreign) / "gen.txt").write_text("agent-foreign\n")
    _feed_displayed_status(controller, displayed)

    controller._term_agents[1] = {
        "cwd": displayed,
        "touched": {
            os.path.realpath(os.path.join(displayed, "edited.py")),
            os.path.realpath(os.path.join(foreign, "gen.txt")),
        },
    }
    controller._focused_term_agent = 1

    # Displayed slice: one file.
    assert controller.focusedAgentChangesCount == 1
    # Foreign probe hasn't landed → no sections yet, total is displayed-only.
    assert controller.focusedAgentForeignChanges == []
    assert controller.focusedAgentChangesTotalCount == 1

    # Hand-deliver the foreign repo's real `git status` snapshot.
    controller._on_foreign_probe_ready(foreign, probe_status_map(foreign))

    sections = controller.focusedAgentForeignChanges
    assert len(sections) == 1
    sec = sections[0]
    assert sec["root"] == foreign
    assert sec["label"] == os.path.basename(foreign)
    assert sec["count"] == 1
    assert os.path.join(foreign, "gen.txt") in sec["pathFilter"]
    # Total now spans both repos.
    assert controller.focusedAgentChangesTotalCount == 2


def test_foreign_status_lookup_matches_section(controller, tmp_path) -> None:
    displayed = _init_repo(tmp_path / "displayed")
    foreign = _init_repo(tmp_path / "foreign")
    controller._cwd = displayed
    _feed_displayed_status(controller, displayed)

    (Path(foreign) / "committed.py").write_text("edited\n")  # modified
    (Path(foreign) / "brand_new.py").write_text("new\n")  # untracked
    controller._term_agents[1] = {
        "cwd": displayed,
        "touched": {
            os.path.realpath(os.path.join(foreign, "committed.py")),
            os.path.realpath(os.path.join(foreign, "brand_new.py")),
        },
    }
    controller._focused_term_agent = 1
    controller._on_foreign_probe_ready(foreign, probe_status_map(foreign))

    # The section carries both foreign files, and the per-repo status lookup
    # returns the right badge char for each (the FileTreeView's statusProvider).
    assert controller.focusedAgentForeignChanges[0]["count"] == 2
    mod = controller.agentForeignStatusForPath(
        foreign, os.path.join(foreign, "committed.py")
    )
    assert mod["char"] == "M"
    unt = controller.agentForeignStatusForPath(
        foreign, os.path.join(foreign, "brand_new.py")
    )
    assert unt["char"] == "?"
    # An uncached root → {} (no badge); an unchanged path likewise.
    assert controller.agentForeignStatusForPath("/nope", "/nope/x") == {}
    assert (
        controller.agentForeignStatusForPath(
            foreign, os.path.join(foreign, "committed.py")
        )["char"]
        == "M"
    )


def test_foreign_prunes_when_focus_leaves(controller, tmp_path) -> None:
    """A foreign section belongs to the FOCUSED agent; focusing an agent that
    touched nothing foreign drops the cached snapshot + empties the sections."""
    displayed = _init_repo(tmp_path / "displayed")
    foreign = _init_repo(tmp_path / "foreign")
    controller._cwd = displayed
    _feed_displayed_status(controller, displayed)

    (Path(foreign) / "gen.txt").write_text("x\n")
    controller._term_agents[1] = {
        "cwd": displayed,
        "touched": {os.path.realpath(os.path.join(foreign, "gen.txt"))},
    }
    controller._term_agents[2] = {"cwd": displayed}  # touched nothing
    controller._focused_term_agent = 1
    controller._on_foreign_probe_ready(foreign, probe_status_map(foreign))
    assert len(controller.focusedAgentForeignChanges) == 1

    # Focus agent 2 (nothing foreign) and re-run the refresh: the stale foreign
    # root is pruned from cache and the sections go empty.
    controller._focused_term_agent = 2
    controller._refresh_foreign_changes()
    assert controller.focusedAgentForeignChanges == []
    assert foreign not in controller._foreign_status_cache


def test_foreign_probe_failure_keeps_prior_snapshot(controller, tmp_path) -> None:
    displayed = _init_repo(tmp_path / "displayed")
    foreign = _init_repo(tmp_path / "foreign")
    controller._cwd = displayed
    _feed_displayed_status(controller, displayed)

    (Path(foreign) / "gen.txt").write_text("x\n")
    controller._term_agents[1] = {
        "cwd": displayed,
        "touched": {os.path.realpath(os.path.join(foreign, "gen.txt"))},
    }
    controller._focused_term_agent = 1
    controller._on_foreign_probe_ready(foreign, probe_status_map(foreign))
    assert len(controller.focusedAgentForeignChanges) == 1

    # A later probe FAILS (None) — the prior snapshot must survive, not wipe.
    controller._on_foreign_probe_ready(foreign, None)
    assert len(controller.focusedAgentForeignChanges) == 1
    assert foreign in controller._foreign_status_cache


def test_foreign_sections_capped_but_total_counts_all(controller, tmp_path) -> None:
    """More foreign repos than the display cap: sections cap, overflow reports
    the rest, and the header TOTAL still counts every foreign file (no silent
    cap)."""
    from symmetria_ide.app import _FOREIGN_SECTION_CAP

    displayed = _init_repo(tmp_path / "displayed")
    controller._cwd = displayed
    _feed_displayed_status(controller, displayed)

    n = _FOREIGN_SECTION_CAP + 2
    foreigns = [_init_repo(tmp_path / f"foreign{i}") for i in range(n)]
    touched = set()
    for fr in foreigns:
        (Path(fr) / "gen.txt").write_text("x\n")
        touched.add(os.path.realpath(os.path.join(fr, "gen.txt")))
    controller._term_agents[1] = {"cwd": displayed, "touched": touched}
    controller._focused_term_agent = 1
    for fr in foreigns:
        controller._on_foreign_probe_ready(fr, probe_status_map(fr))

    assert len(controller.focusedAgentForeignChanges) == _FOREIGN_SECTION_CAP
    assert controller.focusedAgentForeignOverflow == 2
    # Displayed repo is clean for this agent (0); every foreign file counts.
    assert controller.focusedAgentChangesTotalCount == n


def test_foreign_probe_exception_reports_none(controller) -> None:
    """A raised probe (parse/git error) must surface as a None result, not a
    swallowed return — otherwise the root stays wedged in `_foreign_probe_inflight`
    forever and never re-probes (finding C1)."""
    from concurrent.futures import Future

    received: list[tuple[str, object]] = []
    controller._foreignProbeReady.connect(lambda r, m: received.append((r, m)))
    fut: Future = Future()
    fut.set_exception(RuntimeError("boom"))
    controller._emit_foreign_probe("/some/foreign/root", fut)
    assert received == [("/some/foreign/root", None)]


def test_foreign_none_result_clears_inflight(controller, tmp_path) -> None:
    """The None (failure) result discards the root from in-flight so a later
    refresh re-probes it — the unwedging half of finding C1's fix."""
    displayed = _init_repo(tmp_path / "displayed")
    foreign = _init_repo(tmp_path / "foreign")
    controller._cwd = displayed
    (Path(foreign) / "gen.txt").write_text("x\n")
    controller._term_agents[1] = {
        "cwd": displayed,
        "touched": {os.path.realpath(os.path.join(foreign, "gen.txt"))},
    }
    controller._focused_term_agent = 1
    controller._foreign_probe_inflight.add(foreign)
    controller._on_foreign_probe_ready(foreign, None)
    assert foreign not in controller._foreign_probe_inflight
