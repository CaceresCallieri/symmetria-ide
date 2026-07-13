"""Tests for the branches-panel pure layer + list model.

Pure-data tests — no subprocess, no Qt event loop, no temporary repos. We
construct the two wire formats directly as bytes (`git for-each-ref` NUL-field
lines and `git worktree list --porcelain` blocks) and assert the resulting
rows. Model tests drive `_refresh()` directly against a stub controller (same
style as `test_git_log_controller.py`); conftest's bare QCoreApplication is
enough — never pump events (processEvents SEGV memo in `.claude/memory`).

See `src/symmetria_ide/git_branch_controller.py` for the parsers and field
order (`_BRANCH_FORMAT`).
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from symmetria_ide.git_branch_controller import (
    _BRANCH_FIELD_COUNT,
    BranchRow,
    GitBranchController,
    GitBranchListModel,
    WorktreeRow,
    annotate_worktrees,
    format_recency,
    parse_for_each_ref_branches,
    parse_worktree_list_porcelain,
)

_NUL = "\x00"


def _branch(
    *,
    head: str = " ",
    name: str = "main",
    upstream: str = "origin/main",
    track: str = "",
    subject: str = "do a thing",
    sha: str = "0123456789abcdef0123456789abcdef01234567",
    unix: str = "1750000000",
) -> bytes:
    """Build one for-each-ref record: the 7 fields joined by NUL."""
    fields = [head, name, upstream, track, subject, sha, unix]
    assert len(fields) == _BRANCH_FIELD_COUNT
    return _NUL.join(fields).encode("utf-8")


def _join_lines(*records: bytes) -> bytes:
    """Join records with newline — for-each-ref's record separator."""
    return b"\n".join(records)


# ---------------------------------------------------------------------------
# parse_for_each_ref_branches
# ---------------------------------------------------------------------------


def test_branches_empty_blob() -> None:
    assert parse_for_each_ref_branches(b"") == []


def test_branches_all_fields_round_trip() -> None:
    blob = _join_lines(
        _branch(
            head="*",
            name="dev",
            upstream="origin/dev",
            track="[ahead 3]",
            subject="fix: things",
            sha="a" * 40,
            unix="1750000000",
        )
    )
    rows = parse_for_each_ref_branches(blob)
    assert rows == [
        BranchRow(
            name="dev",
            is_head=True,
            upstream="origin/dev",
            ahead=3,
            behind=0,
            upstream_gone=False,
            subject="fix: things",
            sha="a" * 40,
            committer_unix=1750000000,
        )
    ]


def test_branches_head_marker_only_on_star() -> None:
    rows = parse_for_each_ref_branches(
        _join_lines(_branch(head="*", name="dev"), _branch(head=" ", name="main"))
    )
    assert [(r.name, r.is_head) for r in rows] == [("dev", True), ("main", False)]


def test_branches_ahead_behind_variants() -> None:
    rows = parse_for_each_ref_branches(
        _join_lines(
            _branch(name="a", track="[ahead 3]"),
            _branch(name="b", track="[behind 2]"),
            _branch(name="c", track="[ahead 1, behind 4]"),
            _branch(name="d", track=""),
        )
    )
    assert [(r.ahead, r.behind) for r in rows] == [(3, 0), (0, 2), (1, 4), (0, 0)]


def test_branches_gone_upstream() -> None:
    (row,) = parse_for_each_ref_branches(
        _join_lines(_branch(name="stale", upstream="origin/stale", track="[gone]"))
    )
    assert row.upstream_gone is True
    assert (row.ahead, row.behind) == (0, 0)


def test_branches_no_upstream() -> None:
    (row,) = parse_for_each_ref_branches(
        _join_lines(_branch(name="local-only", upstream="", track=""))
    )
    assert row.upstream == ""
    assert row.upstream_gone is False


def test_branches_warning_line_skipped_neighbors_survive() -> None:
    # git can interleave warning lines into stdout (lazygit issue #1385) —
    # they have no NUL fields, so the column-count guard drops them.
    blob = _join_lines(
        _branch(name="alpha"),
        b"warning: refname 'x' is ambiguous",
        _branch(name="beta"),
    )
    rows = parse_for_each_ref_branches(blob)
    assert [r.name for r in rows] == ["alpha", "beta"]


def test_branches_heads_prefix_stripped() -> None:
    # `refname:short` yields "heads/<name>" when a tag shares the name.
    (row,) = parse_for_each_ref_branches(_join_lines(_branch(name="heads/v1")))
    assert row.name == "v1"


def test_branches_unicode_name() -> None:
    (row,) = parse_for_each_ref_branches(_join_lines(_branch(name="función-β")))
    assert row.name == "función-β"


def test_branches_bad_unix_date_clamps_to_zero() -> None:
    (row,) = parse_for_each_ref_branches(_join_lines(_branch(unix="not-a-number")))
    assert row.committer_unix == 0


# ---------------------------------------------------------------------------
# parse_worktree_list_porcelain
# ---------------------------------------------------------------------------


def test_worktrees_two_worktree_happy_path() -> None:
    blob = (
        b"worktree /home/jc/projects/symmetria-ide\n"
        b"HEAD " + b"a" * 40 + b"\n"
        b"branch refs/heads/dev\n"
        b"\n"
        b"worktree /home/jc/projects/symmetria-ide-stable\n"
        b"HEAD " + b"b" * 40 + b"\n"
        b"branch refs/heads/main\n"
        b"\n"
    )
    rows = parse_worktree_list_porcelain(blob)
    assert rows == [
        WorktreeRow(
            path="/home/jc/projects/symmetria-ide",
            head_sha="a" * 40,
            branch="dev",
        ),
        WorktreeRow(
            path="/home/jc/projects/symmetria-ide-stable",
            head_sha="b" * 40,
            branch="main",
        ),
    ]


def test_worktrees_detached_record_has_empty_branch() -> None:
    blob = b"worktree /w/detached\nHEAD " + b"c" * 40 + b"\ndetached\n"
    (row,) = parse_worktree_list_porcelain(blob)
    assert row.branch == ""
    assert row.is_detached is True


def test_worktrees_bare_record_discarded() -> None:
    blob = (
        b"worktree /srv/repo.git\n"
        b"bare\n"
        b"\n"
        b"worktree /w/one\n"
        b"HEAD " + b"d" * 40 + b"\n"
        b"branch refs/heads/main\n"
    )
    rows = parse_worktree_list_porcelain(blob)
    assert [r.path for r in rows] == ["/w/one"]


def test_worktrees_trailing_blank_lines() -> None:
    blob = b"worktree /w/one\nHEAD " + b"e" * 40 + b"\nbranch refs/heads/main\n\n\n\n"
    rows = parse_worktree_list_porcelain(blob)
    assert len(rows) == 1


def test_worktrees_slashy_branch_name() -> None:
    blob = b"worktree /w/feat\nHEAD " + b"f" * 40 + b"\nbranch refs/heads/feature/x\n"
    (row,) = parse_worktree_list_porcelain(blob)
    assert row.branch == "feature/x"


def test_worktrees_empty_blob() -> None:
    assert parse_worktree_list_porcelain(b"") == []


# ---------------------------------------------------------------------------
# format_recency
# ---------------------------------------------------------------------------


def test_format_recency_table() -> None:
    now = 1_750_000_000
    month = (365 * 24 * 3600) // 12
    cases = [
        (now - 30, "30s"),
        (now - 5 * 60, "5m"),
        (now - 3 * 3600, "3h"),
        (now - 2 * 86400, "2d"),
        (now - 3 * 7 * 86400, "3w"),
        (now - month, "1M"),
        (now - 11 * month, "11M"),
        (now - 12 * month, "1y"),  # 12 months roll into a year exactly
        (now - 2 * 365 * 86400, "2y"),
        (now + 60, "0s"),  # future timestamp (clock skew) clamps
    ]
    for committer_unix, expected in cases:
        assert format_recency(committer_unix, now) == expected, expected


# ---------------------------------------------------------------------------
# annotate_worktrees
# ---------------------------------------------------------------------------


def _mk_branch(name: str, *, is_head: bool = False) -> BranchRow:
    return BranchRow(
        name=name,
        is_head=is_head,
        upstream="",
        ahead=0,
        behind=0,
        upstream_gone=False,
        subject="",
        sha="0" * 40,
        committer_unix=0,
    )


def test_annotate_the_daily_two_worktree_case() -> None:
    # main is checked out at the stable worktree; current root is the dev one.
    branches = [_mk_branch("dev", is_head=True), _mk_branch("main")]
    worktrees = [
        WorktreeRow(path="/p/symmetria-ide", head_sha="a" * 40, branch="dev"),
        WorktreeRow(path="/p/symmetria-ide-stable", head_sha="b" * 40, branch="main"),
    ]
    rows = annotate_worktrees(branches, worktrees, "/p/symmetria-ide")
    by_name = {r.name: r for r in rows}
    assert by_name["dev"].worktree_path == "/p/symmetria-ide"
    assert by_name["main"].worktree_path == "/p/symmetria-ide-stable"
    assert len(rows) == 2  # no detached synthesis — dev is head


def test_annotate_branch_without_worktree_untouched() -> None:
    branches = [_mk_branch("feature", is_head=False), _mk_branch("dev", is_head=True)]
    worktrees = [WorktreeRow(path="/p/one", head_sha="a" * 40, branch="dev")]
    rows = annotate_worktrees(branches, worktrees, "/p/one")
    assert {r.name: r.worktree_path for r in rows} == {
        "feature": "",
        "dev": "/p/one",
    }


def test_annotate_head_branch_moves_to_front() -> None:
    # lazygit's Head-first rule: the panel caps its visible height, so the
    # checked-out branch must never scroll out of the initial view — even
    # when recency sorting (or a committerdate tie) puts it further down.
    branches = [
        _mk_branch("alpha"),
        _mk_branch("beta"),
        _mk_branch("dev", is_head=True),
    ]
    rows = annotate_worktrees(branches, [], "/p/one")
    assert [r.name for r in rows] == ["dev", "alpha", "beta"]


def test_annotate_detached_head_synthesis() -> None:
    # No branch is head → detached HEAD; sha comes from the current worktree.
    branches = [_mk_branch("main")]
    worktrees = [
        WorktreeRow(path="/p/one", head_sha="abcdef0123" + "0" * 30, branch=""),
    ]
    rows = annotate_worktrees(branches, worktrees, "/p/one")
    assert rows[0].detached is True
    assert rows[0].is_head is True
    assert rows[0].name == "(detached abcdef01)"
    assert rows[0].worktree_path == "/p/one"
    assert rows[1].name == "main"


def test_annotate_detached_head_without_matching_worktree() -> None:
    rows = annotate_worktrees([_mk_branch("main")], [], "/p/one")
    assert rows[0].detached is True
    assert rows[0].name == "(detached)"
    assert rows[0].worktree_path == ""


# ---------------------------------------------------------------------------
# GitBranchListModel — driven directly, no event loop
# ---------------------------------------------------------------------------


class _StubBranchController(QObject):
    """Minimal stand-in: the `branchesChanged` signal the model connects to,
    plus settable `branches()` / `repoRoot` snapshots. We never emit the
    signal — tests call `model._refresh()` directly."""

    branchesChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[BranchRow] = []
        self.repoRoot = ""

    def set_rows(self, rows: list[BranchRow]) -> None:
        self._rows = rows

    def branches(self) -> list[BranchRow]:
        return list(self._rows)


def _make_model() -> tuple[GitBranchListModel, _StubBranchController, dict]:
    ctrl = _StubBranchController()
    model = GitBranchListModel(ctrl)
    spy: dict = {"resets": 0}
    model.modelReset.connect(lambda: spy.__setitem__("resets", spy["resets"] + 1))
    return model, ctrl, spy


def _role(model: GitBranchListModel, row: int, name: bytes):
    roles = {v: k for k, v in model.roleNames().items()}
    return model.data(model.index(row, 0), roles[name])


def test_model_refresh_resolves_roles() -> None:
    model, ctrl, spy = _make_model()
    ctrl.repoRoot = "/p/symmetria-ide"
    ctrl.set_rows(
        [
            BranchRow(
                name="main",
                is_head=False,
                upstream="origin/main",
                ahead=2,
                behind=1,
                upstream_gone=False,
                subject="tip subject",
                sha="a" * 40,
                committer_unix=0,
                worktree_path="/p/symmetria-ide-stable",
            )
        ]
    )
    model._refresh()
    assert spy["resets"] == 1
    assert model.rowCount() == 1
    assert _role(model, 0, b"name") == "main"
    assert _role(model, 0, b"isHead") is False
    assert _role(model, 0, b"hasUpstream") is True
    assert _role(model, 0, b"ahead") == 2
    assert _role(model, 0, b"behind") == 1
    assert _role(model, 0, b"worktreeName") == "symmetria-ide-stable"
    assert _role(model, 0, b"checkedOutElsewhere") is True
    assert _role(model, 0, b"recency") == ""  # committer_unix == 0 → blank


def test_model_checked_out_here_is_not_elsewhere() -> None:
    model, ctrl, _ = _make_model()
    ctrl.repoRoot = "/p/symmetria-ide"
    ctrl.set_rows(
        [
            BranchRow(
                name="dev",
                is_head=True,
                upstream="",
                ahead=0,
                behind=0,
                upstream_gone=False,
                subject="",
                sha="b" * 40,
                committer_unix=0,
                worktree_path="/p/symmetria-ide",
            )
        ]
    )
    model._refresh()
    assert _role(model, 0, b"checkedOutElsewhere") is False
    assert _role(model, 0, b"hasUpstream") is False


def test_model_refresh_no_change_is_noop() -> None:
    model, ctrl, spy = _make_model()
    ctrl.set_rows([_mk_branch("main", is_head=True)])
    model._refresh()
    model._refresh()
    assert spy["resets"] == 1


def test_model_gone_upstream_still_has_upstream() -> None:
    # `[gone]` empties %(upstream:short) in some git versions; the panel must
    # still know the branch WAS tracking so it can render the gone state.
    model, ctrl, _ = _make_model()
    ctrl.set_rows(
        [
            BranchRow(
                name="stale",
                is_head=False,
                upstream="",
                ahead=0,
                behind=0,
                upstream_gone=True,
                subject="",
                sha="c" * 40,
                committer_unix=0,
            )
        ]
    )
    model._refresh()
    assert _role(model, 0, b"hasUpstream") is True
    assert _role(model, 0, b"upstreamGone") is True


# ---------------------------------------------------------------------------
# GitBranchController facade — worker stopped, synchronous state transitions
# ---------------------------------------------------------------------------


def _make_stopped_controller() -> GitBranchController:
    """A real controller with its worker stopped immediately — `set_repo_root`
    / `reload` state transitions are synchronous on the caller side, so
    everything below asserts without an event loop or a live worker."""
    ctrl = GitBranchController()
    ctrl.stop()
    return ctrl


def test_set_repo_root_clears_and_enqueues() -> None:
    ctrl = _make_stopped_controller()
    with ctrl._lock:
        ctrl._branches = [_mk_branch("stale")]
        ctrl._reload_pending = True

    ctrl.set_repo_root("/p/new")

    assert ctrl.branches() == []
    assert ctrl._reload_pending is False
    assert ctrl._queue.get_nowait() == ("branches", "/p/new")


def test_set_repo_root_idempotent_on_equal_value() -> None:
    ctrl = _make_stopped_controller()
    ctrl.set_repo_root("/p/repo")
    ctrl._queue.get_nowait()
    ctrl.set_repo_root("/p/repo")
    assert ctrl._queue.empty()


def test_reload_is_coalesced() -> None:
    ctrl = _make_stopped_controller()
    ctrl.set_repo_root("/p/repo")
    ctrl._queue.get_nowait()

    ctrl.reload()
    ctrl.reload()  # second call gated by _reload_pending

    assert ctrl._queue.get_nowait() == ("branches", "/p/repo")
    assert ctrl._queue.empty()


def test_reload_without_repo_is_noop() -> None:
    ctrl = _make_stopped_controller()
    ctrl.reload()
    assert ctrl._queue.empty()


class _FakeExecutor:
    """Executor stand-in (the seam the scan reads — see set_executor)."""

    def __init__(self, resolved: str, run_git_fn) -> None:
        self._resolved = resolved
        self._run_git_fn = run_git_fn

    def resolve_repo_root(self, asked: str) -> str:
        return self._resolved

    def run_git(self, cwd: str, *args, timeout) -> bytes:
        return self._run_git_fn(cwd, *args, timeout=timeout)


def test_do_branches_race_guard_drops_stale_root() -> None:
    # A scan for the OLD root finishing after a project switch must not land.
    ctrl = _make_stopped_controller()
    ctrl.set_repo_root("/p/new")
    ctrl._queue.get_nowait()

    ctrl._executor = _FakeExecutor(
        "/p/old",
        lambda *a, **k: _join_lines(_branch(name="stale-branch")),
    )

    ctrl._do_branches(asked_root="/p/old")

    assert ctrl.branches() == []
    assert ctrl.repoRoot == ""


def test_do_branches_applies_for_current_root() -> None:
    ctrl = _make_stopped_controller()
    ctrl.set_repo_root("/p/repo")
    ctrl._queue.get_nowait()

    def fake_run_git(cwd, *args, timeout):
        if args[0] == "for-each-ref":
            return _join_lines(_branch(head="*", name="dev"))
        return b""  # empty worktree list

    ctrl._executor = _FakeExecutor("/p/repo", fake_run_git)

    ctrl._do_branches(asked_root="/p/repo")

    assert [b.name for b in ctrl.branches()] == ["dev"]
    assert ctrl.repoRoot == "/p/repo"
    # The coalescing flag was cleared at the top of the scan.
    assert ctrl._reload_pending is False
