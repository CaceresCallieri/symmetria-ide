"""Parser tests for `git log -z --pretty=format:<_LOG_FORMAT>` output.

Pure-data tests — no subprocess, no Qt, no temporary repos. We construct log
payloads directly as bytes (the wire format the parser ingests) and assert the
resulting `list[CommitRow]`. Mirrors the porcelain-parser test style in
`test_git_controller.py`: intent-revealing fixture helpers keep each case
reading like "this record shape produces this commit."

See `src/symmetria_ide/git_log_controller.py` for the parser and the field
order (`_LOG_FORMAT`).
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from symmetria_ide.git_log_controller import (
    _LOG_FIELD_COUNT,
    CommitRow,
    GitLogController,
    GitLogListModel,
    parse_git_log,
)

# Control characters the format uses — kept as locals so the fixtures read
# literally against the parser's `_FIELD_SEP` / `_RECORD_SEP`.
_US = "\x1f"  # field separator (within a commit)
_NUL = "\x00"  # record separator (between commits, git's `-z`)


def _commit(
    *,
    full_hash: str = "0123456789abcdef0123456789abcdef01234567",
    abbrev: str = "0123456",
    parents: str = "aaaaaaa",
    author_name: str = "jc",
    author_email: str = "jc@example.com",
    date_iso: str = "2026-06-15T12:00:00+00:00",
    relative: str = "2 hours ago",
    subject: str = "do a thing",
    body: str = "",
    refs: str = "",
    trailer: str = "",
) -> bytes:
    """Build one commit record: the 11 fields joined by the unit separator."""
    fields = [
        full_hash,
        abbrev,
        parents,
        author_name,
        author_email,
        date_iso,
        relative,
        subject,
        body,
        refs,
        trailer,
    ]
    assert len(fields) == _LOG_FIELD_COUNT
    return _US.join(fields).encode("utf-8")


def _join(*records: bytes) -> bytes:
    """Join commit records with NUL — matches `git log -z` separator semantics."""
    return _NUL.encode("utf-8").join(records)


# ---------------------------------------------------------------------------
# Empty / degenerate inputs
# ---------------------------------------------------------------------------


def test_empty_blob_returns_empty_list() -> None:
    assert parse_git_log(b"") == []


def test_only_separators_returns_empty_list() -> None:
    assert parse_git_log(_NUL.encode() * 3) == []


# ---------------------------------------------------------------------------
# Single commit — every field round-trips
# ---------------------------------------------------------------------------


def test_single_commit_parses_all_fields() -> None:
    blob = _commit(
        full_hash="f" * 40,
        abbrev="fffffff",
        parents="1111111",
        author_name="Ada Lovelace",
        author_email="ada@example.com",
        date_iso="2026-01-02T03:04:05+00:00",
        relative="3 days ago",
        subject="feat: add the analytical engine",
        body="Longer rationale here.",
        refs="HEAD -> dev, origin/dev",
    )
    rows = parse_git_log(blob)
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, CommitRow)
    assert row.hash == "f" * 40
    assert row.abbrev_hash == "fffffff"
    assert row.parents == ("1111111",)
    assert row.author_name == "Ada Lovelace"
    assert row.author_email == "ada@example.com"
    assert row.author_date_iso == "2026-01-02T03:04:05+00:00"
    assert row.relative_date == "3 days ago"
    assert row.subject == "feat: add the analytical engine"
    assert row.body == "Longer rationale here."
    assert row.refs == "HEAD -> dev, origin/dev"
    # Future-proof seams default empty / false in v0.
    assert row.agent_id == ""
    assert row.additions == 0
    assert row.deletions == 0
    assert row.seen is False


# ---------------------------------------------------------------------------
# Parents — root, single, merge
# ---------------------------------------------------------------------------


def test_root_commit_has_no_parents() -> None:
    rows = parse_git_log(_commit(parents=""))
    assert rows[0].parents == ()


def test_single_parent() -> None:
    rows = parse_git_log(_commit(parents="abc1234"))
    assert rows[0].parents == ("abc1234",)


def test_merge_commit_has_multiple_parents() -> None:
    # `%P` emits parent hashes space-separated — a 2-parent merge, the case
    # the native graph ribbon (Phase 3) will route lanes for.
    rows = parse_git_log(_commit(parents="aaaaaaa bbbbbbb"))
    assert rows[0].parents == ("aaaaaaa", "bbbbbbb")


def test_octopus_merge_three_parents() -> None:
    rows = parse_git_log(_commit(parents="aaa bbb ccc"))
    assert rows[0].parents == ("aaa", "bbb", "ccc")


# ---------------------------------------------------------------------------
# Ordering, multiplicity, trailing separator robustness
# ---------------------------------------------------------------------------


def test_multiple_commits_preserve_order() -> None:
    blob = _join(
        _commit(abbrev="aaaaaaa", subject="newest"),
        _commit(abbrev="bbbbbbb", subject="middle"),
        _commit(abbrev="ccccccc", subject="oldest"),
    )
    rows = parse_git_log(blob)
    assert [r.abbrev_hash for r in rows] == ["aaaaaaa", "bbbbbbb", "ccccccc"]
    assert [r.subject for r in rows] == ["newest", "middle", "oldest"]


def test_trailing_separator_does_not_yield_phantom_row() -> None:
    # Some git versions terminate (rather than separate) entries under `-z`;
    # the parser must filter the resulting empty trailing chunk.
    blob = _join(_commit(subject="a"), _commit(subject="b")) + _NUL.encode()
    rows = parse_git_log(blob)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Body — empty, multi-line, trailing-newline stripping
# ---------------------------------------------------------------------------


def test_empty_body() -> None:
    rows = parse_git_log(_commit(body=""))
    assert rows[0].body == ""


def test_multiline_body_preserved() -> None:
    body = "First paragraph.\n\nSecond paragraph with detail.\n"
    rows = parse_git_log(_commit(body=body))
    # Interior newlines preserved; only the trailing newline is stripped.
    assert rows[0].body == "First paragraph.\n\nSecond paragraph with detail."


# ---------------------------------------------------------------------------
# Unicode
# ---------------------------------------------------------------------------


def test_unicode_in_subject_and_author() -> None:
    rows = parse_git_log(
        _commit(author_name="José Núñez", subject="fix: café ☕ rendering")
    )
    assert rows[0].author_name == "José Núñez"
    assert rows[0].subject == "fix: café ☕ rendering"


# ---------------------------------------------------------------------------
# Agent-correlation seam — the Symmetria-Agent-Id trailer
# ---------------------------------------------------------------------------


def test_agent_id_trailer_populates_field() -> None:
    rows = parse_git_log(_commit(trailer="12345_2"))
    assert rows[0].agent_id == "12345_2"


def test_absent_agent_trailer_is_empty() -> None:
    rows = parse_git_log(_commit(trailer=""))
    assert rows[0].agent_id == ""


def test_agent_id_trailer_whitespace_stripped() -> None:
    rows = parse_git_log(_commit(trailer="  98765_4  "))
    assert rows[0].agent_id == "98765_4"


# ---------------------------------------------------------------------------
# Malformed records
# ---------------------------------------------------------------------------


def test_record_with_too_few_fields_is_skipped() -> None:
    # A truncated record (fewer than 11 fields) is dropped, not crashed on; a
    # well-formed neighbour still parses.
    truncated = _US.join(["abc", "abc", ""]).encode("utf-8")
    blob = _join(truncated, _commit(subject="survives"))
    rows = parse_git_log(blob)
    assert len(rows) == 1
    assert rows[0].subject == "survives"


# ---------------------------------------------------------------------------
# GitLogListModel._refresh — the append-vs-reset splice logic (the risk area:
# off-by-one in beginInsertRows, prefix-equality, the no-op short-circuit). We
# drive _refresh directly with a stub controller and observe which model
# signal it emits (rowsInserted for a pure append, modelReset otherwise).
# ---------------------------------------------------------------------------


class _StubLogController(QObject):
    """Minimal stand-in: the `logChanged` signal the model connects to in its
    constructor, plus a settable `commits()` snapshot. We never emit
    `logChanged` — tests call `model._refresh()` directly."""

    logChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[CommitRow] = []

    def set_rows(self, rows: list[CommitRow]) -> None:
        self._rows = rows

    def commits(self) -> list[CommitRow]:
        return list(self._rows)


def _row(h: str) -> CommitRow:
    """A CommitRow keyed by hash — enough for identity/equality in the model."""
    return CommitRow(
        hash=h,
        abbrev_hash=h[:7],
        parents=(),
        author_name="x",
        author_email="x@example.com",
        author_date_iso="",
        relative_date="",
        subject=h,
        body="",
        refs="",
    )


def _make_model() -> tuple[GitLogListModel, _StubLogController, dict]:
    """Build a model over a stub, wired with signal spies for inserts/resets."""
    ctrl = _StubLogController()
    model = GitLogListModel(ctrl)
    spy: dict = {"inserted": [], "resets": 0}
    model.rowsInserted.connect(
        lambda _parent, first, last: spy["inserted"].append((first, last))
    )
    model.modelReset.connect(lambda: spy.__setitem__("resets", spy["resets"] + 1))
    return model, ctrl, spy


def test_refresh_fresh_load_is_a_reset() -> None:
    # First populate from empty → full reset (not an append: old_len == 0).
    model, ctrl, spy = _make_model()
    ctrl.set_rows([_row("a"), _row("b"), _row("c")])
    model._refresh()
    assert model.rowCount() == 3
    assert spy["resets"] == 1
    assert spy["inserted"] == []


def test_refresh_pure_append_uses_insert_rows() -> None:
    # Extending the existing prefix → beginInsertRows over the new tail only,
    # so the ListView keeps its scroll position. Range must be [old_len, new-1].
    model, ctrl, spy = _make_model()
    ctrl.set_rows([_row("a"), _row("b"), _row("c")])
    model._refresh()
    spy["resets"] = 0
    spy["inserted"].clear()

    ctrl.set_rows([_row("a"), _row("b"), _row("c"), _row("d"), _row("e")])
    model._refresh()
    assert model.rowCount() == 5
    assert spy["inserted"] == [(3, 4)]  # off-by-one guard: rows 3 and 4 inserted
    assert spy["resets"] == 0


def test_refresh_identical_list_is_a_noop() -> None:
    # Same content → no signal at all (the equality short-circuit).
    model, ctrl, spy = _make_model()
    rows = [_row("a"), _row("b")]
    ctrl.set_rows(rows)
    model._refresh()
    spy["resets"] = 0
    spy["inserted"].clear()

    ctrl.set_rows(list(rows))  # equal-but-distinct list
    model._refresh()
    assert model.rowCount() == 2
    assert spy["inserted"] == []
    assert spy["resets"] == 0


def test_refresh_divergent_prefix_is_a_reset() -> None:
    # New list does not extend the old prefix → full reset, not an append.
    model, ctrl, spy = _make_model()
    ctrl.set_rows([_row("a"), _row("b"), _row("c")])
    model._refresh()
    spy["resets"] = 0
    spy["inserted"].clear()

    ctrl.set_rows([_row("x"), _row("y")])  # different repo / reload
    model._refresh()
    assert model.rowCount() == 2
    assert spy["resets"] == 1
    assert spy["inserted"] == []


def test_refresh_shorter_list_is_a_reset() -> None:
    # A shrink (e.g. switch to a repo with fewer commits) can't be an append.
    model, ctrl, spy = _make_model()
    ctrl.set_rows([_row("a"), _row("b"), _row("c"), _row("d")])
    model._refresh()
    spy["resets"] = 0
    spy["inserted"].clear()

    ctrl.set_rows([_row("a"), _row("b")])
    model._refresh()
    assert model.rowCount() == 2
    assert spy["resets"] == 1
    assert spy["inserted"] == []


# ---------------------------------------------------------------------------
# Ref filtering (`set_ref` / `_run_log` ref argument)
# ---------------------------------------------------------------------------


def _make_stopped_controller() -> GitLogController:
    """A real controller with its worker stopped immediately — `set_ref` /
    `set_repo_root` state transitions are synchronous on the caller side, so
    everything below asserts without an event loop or a live worker."""
    ctrl = GitLogController()
    ctrl.stop()
    return ctrl


def test_run_log_ref_appends_ref_and_double_dash(monkeypatch) -> None:
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv

        class _P:
            returncode = 0
            stdout = b""
            stderr = b""

        return _P()

    ctrl = _make_stopped_controller()
    monkeypatch.setattr("symmetria_ide.git_subprocess.subprocess.run", fake_run)
    ctrl._run_log("/tmp", skip=0, ref="feature-x")
    assert captured["argv"][-2:] == ["feature-x", "--"]


def test_run_log_empty_ref_keeps_argv_unchanged(monkeypatch) -> None:
    # Regression pin: the unfiltered command must stay byte-identical to the
    # pre-filter behavior (HEAD implicit, no trailing args).
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv

        class _P:
            returncode = 0
            stdout = b""
            stderr = b""

        return _P()

    ctrl = _make_stopped_controller()
    monkeypatch.setattr("symmetria_ide.git_subprocess.subprocess.run", fake_run)
    ctrl._run_log("/tmp", skip=7, ref="")
    assert captured["argv"] == [
        "git",
        "log",
        "-z",
        "--no-color",
        "--max-count=100",
        "--skip=7",
        captured["argv"][6],  # the --pretty format arg (content pinned below)
    ]
    assert captured["argv"][6].startswith("--pretty=format:")


def test_set_ref_clears_commits_and_commit_diff() -> None:
    ctrl = _make_stopped_controller()
    ctrl._repo_root = "/p/repo"
    with ctrl._lock:
        ctrl._commits = [_row("a")]
        ctrl._has_more = True
        ctrl._current_diff_hash = "a" * 40
        ctrl._current_diff_text = "patch"
        ctrl._current_file_diff_path = "f.py"
        ctrl._current_file_diff_text = "file patch"

    ctrl.set_ref("feature-x")

    assert ctrl.currentRef == "feature-x"
    assert ctrl.commits() == []
    assert ctrl.hasMore is False
    assert ctrl.currentDiffHash == ""
    assert ctrl.currentDiffText == ""
    # File-diff fields are working-tree-scoped — a ref switch keeps them.
    assert ctrl.currentFileDiffPath == "f.py"
    assert ctrl.currentFileDiffText == "file patch"
    # The enqueued page-0 request carries the new ref.
    assert ctrl._queue.get_nowait() == ("log", "/p/repo", 0, "feature-x")


def test_set_ref_idempotent_on_equal_value() -> None:
    ctrl = _make_stopped_controller()
    ctrl._repo_root = "/p/repo"
    ctrl.set_ref("dev")
    ctrl._queue.get_nowait()  # drain the first request
    ctrl.set_ref("dev")
    assert ctrl._queue.empty()


def test_set_repo_root_resets_ref_to_head() -> None:
    ctrl = _make_stopped_controller()
    ctrl._repo_root = "/p/old"
    ctrl.set_ref("feature-x")
    ctrl._queue.get_nowait()

    ctrl.set_repo_root("/p/new")

    assert ctrl.currentRef == ""
    assert ctrl._queue.get_nowait() == ("log", "/p/new", 0, "")
