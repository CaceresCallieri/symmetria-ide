"""Parser tests for `git status --porcelain=v2 -z` output.

Pure-data tests — no subprocess, no Qt, no temporary repos. We construct
porcelain payloads directly as bytes (the wire format the parser ingests)
and assert the resulting `dict[str, GitStatus]`. The fixture helpers below
keep the bytes intent-revealing so each test case reads like "this record
shape produces this status."

See `src/symmetria_ide/git_controller.py` for the parser itself; the
format spec is summarised in its module docstring.
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex

from symmetria_ide.git_controller import (
    STATE_CONFLICTED,
    STATE_IGNORED,
    STATE_RENAMED,
    STATE_STAGED,
    STATE_UNSTAGED,
    STATE_UNTRACKED,
    GitController,
    GitStats,
    GitStatus,
    GitStatusListModel,
    _add_directory_aggregates,
    _compute_stats,
    _merge_numstat_into_map,
    parse_numstat_blob,
    parse_porcelain_v2,
)


# ---------------------------------------------------------------------------
# Fixture helpers — build NUL-terminated porcelain records.
# ---------------------------------------------------------------------------


def _ordinary(xy: str, path: str) -> bytes:
    """One ordinary changed entry (porcelain record type `1`).

    Mode/hash fields are filled with placeholders — the parser doesn't
    read them, so realistic-looking values would only obscure intent.
    """
    return f"1 {xy} N... 100644 100644 100644 abc123 def456 {path}".encode("utf-8")


def _rename(xy: str, new_path: str, orig_path: str, score: str = "R100") -> bytes:
    """One rename/copy entry (record type `2`) — emits TWO NUL fields.

    Returns the bytes WITHOUT a trailing NUL between the two fields; the
    caller joins records with NUL separators so the embedded NUL between
    `new_path` and `orig_path` falls out naturally.
    """
    head = f"2 {xy} N... 100644 100644 100644 abc123 def456 {score} {new_path}".encode(
        "utf-8"
    )
    return head + b"\x00" + orig_path.encode("utf-8")


def _unmerged(xy: str, path: str) -> bytes:
    return f"u {xy} N... 100644 100644 100644 100644 h1 h2 h3 {path}".encode("utf-8")


def _untracked(path: str) -> bytes:
    return f"? {path}".encode("utf-8")


def _ignored(path: str) -> bytes:
    return f"! {path}".encode("utf-8")


def _join(*records: bytes) -> bytes:
    """Join records with NUL separators and a trailing NUL — matches real output."""
    return b"\x00".join(records) + b"\x00"


# ---------------------------------------------------------------------------
# Empty / header-only inputs
# ---------------------------------------------------------------------------


def test_empty_blob_returns_empty_map() -> None:
    assert parse_porcelain_v2(b"") == {}


def test_only_branch_headers_returns_empty_map() -> None:
    blob = _join(
        b"# branch.oid abc123",
        b"# branch.head main",
        b"# branch.upstream origin/main",
        b"# branch.ab +0 -0",
    )
    assert parse_porcelain_v2(blob) == {}


def test_missing_trailing_nul_still_parses() -> None:
    # Real `git status -z` always emits a trailing NUL, but defending against
    # the absent case keeps the parser robust to partial pipe reads.
    blob = _ordinary(".M", "foo.txt")
    result = parse_porcelain_v2(blob)
    assert "foo.txt" in result


# ---------------------------------------------------------------------------
# Ordinary entries — the worktree-precedence rule is the load-bearing case.
# ---------------------------------------------------------------------------


def test_modified_in_worktree_only_is_unstaged() -> None:
    blob = _join(_ordinary(".M", "src/foo.py"))
    result = parse_porcelain_v2(blob)
    assert result == {
        "src/foo.py": GitStatus(
            path="src/foo.py",
            char="M",
            state=STATE_UNSTAGED,
            tooltip="Modified",
        ),
    }


def test_modified_in_index_only_is_staged() -> None:
    blob = _join(_ordinary("M.", "src/foo.py"))
    status = parse_porcelain_v2(blob)["src/foo.py"]
    assert status.char == "M"
    assert status.state == STATE_STAGED
    assert status.tooltip == "Modified (staged)"


def test_modified_both_sides_worktree_takes_precedence() -> None:
    # "MM" = staged then modified again. LazyGit convention: show the
    # worktree state (unstaged red) because that's the more recent action.
    blob = _join(_ordinary("MM", "src/foo.py"))
    status = parse_porcelain_v2(blob)["src/foo.py"]
    assert status.char == "M"
    assert status.state == STATE_UNSTAGED


def test_added_staged() -> None:
    blob = _join(_ordinary("A.", "new.py"))
    status = parse_porcelain_v2(blob)["new.py"]
    assert status.char == "A"
    assert status.state == STATE_STAGED
    assert status.tooltip == "Added (staged)"


def test_deleted_in_worktree_is_unstaged() -> None:
    blob = _join(_ordinary(".D", "removed.py"))
    status = parse_porcelain_v2(blob)["removed.py"]
    assert status.char == "D"
    assert status.state == STATE_UNSTAGED


def test_deleted_staged() -> None:
    blob = _join(_ordinary("D.", "removed.py"))
    status = parse_porcelain_v2(blob)["removed.py"]
    assert status.char == "D"
    assert status.state == STATE_STAGED


def test_type_changed_in_worktree() -> None:
    blob = _join(_ordinary(".T", "link"))
    status = parse_porcelain_v2(blob)["link"]
    assert status.char == "T"
    assert status.state == STATE_UNSTAGED


# ---------------------------------------------------------------------------
# Rename / copy entries — type `2` records consume TWO NUL fields.
# ---------------------------------------------------------------------------


def test_renamed_captures_both_paths() -> None:
    blob = _join(_rename("R.", "new_name.py", "old_name.py"))
    status = parse_porcelain_v2(blob)["new_name.py"]
    assert status.char == "R"
    assert status.state == STATE_RENAMED
    assert status.orig_path == "old_name.py"
    assert status.tooltip == "Renamed"


def test_copied_uses_renamed_palette() -> None:
    # Per FM brief: rename + copy share the yellow palette ("renamed/copied").
    blob = _join(_rename("C.", "copy.py", "source.py"))
    status = parse_porcelain_v2(blob)["copy.py"]
    assert status.char == "C"
    assert status.state == STATE_RENAMED
    assert status.orig_path == "source.py"
    assert status.tooltip == "Copied"


def test_rename_does_not_break_following_record() -> None:
    # The `2` parser advances `i` by 2 (record + origPath). If that's wrong,
    # the next ordinary record gets misaligned. This test pins the cursor.
    blob = _join(
        _rename("R.", "renamed.py", "original.py"),
        _ordinary(".M", "follower.py"),
    )
    result = parse_porcelain_v2(blob)
    assert set(result.keys()) == {"renamed.py", "follower.py"}
    assert result["follower.py"].state == STATE_UNSTAGED


# ---------------------------------------------------------------------------
# Other record types
# ---------------------------------------------------------------------------


def test_untracked() -> None:
    blob = _join(_untracked("new_file.txt"))
    status = parse_porcelain_v2(blob)["new_file.txt"]
    assert status.char == "?"
    assert status.state == STATE_UNTRACKED
    assert status.tooltip == "Untracked"
    assert status.orig_path is None


def test_ignored() -> None:
    blob = _join(_ignored("build/output"))
    status = parse_porcelain_v2(blob)["build/output"]
    assert status.char == "!"
    assert status.state == STATE_IGNORED


def test_unmerged_conflict() -> None:
    blob = _join(_unmerged("UU", "conflicted.py"))
    status = parse_porcelain_v2(blob)["conflicted.py"]
    assert status.char == "U"
    assert status.state == STATE_CONFLICTED
    assert status.tooltip == "Conflicted"


# ---------------------------------------------------------------------------
# Path edge cases — spaces and UTF-8
# ---------------------------------------------------------------------------


def test_path_with_spaces() -> None:
    # Porcelain v2 -z separates path from preceding fields with a single
    # space; everything after that space (until the NUL terminator) IS the
    # path, including embedded spaces. Our split(' ', N) limit is what
    # makes this work — N spaces consumed, the rest is one field.
    blob = _join(_ordinary(".M", "dir with spaces/file name.txt"))
    result = parse_porcelain_v2(blob)
    assert "dir with spaces/file name.txt" in result


def test_utf8_path() -> None:
    blob = _join(_ordinary(".M", "café/résumé.md"))
    assert "café/résumé.md" in parse_porcelain_v2(blob)


def test_rename_with_spaces_in_both_paths() -> None:
    blob = _join(_rename("R.", "new path/with spaces.py", "old path/with spaces.py"))
    status = parse_porcelain_v2(blob)["new path/with spaces.py"]
    assert status.orig_path == "old path/with spaces.py"


# ---------------------------------------------------------------------------
# Multi-record + branch-header mixing — the real-world shape
# ---------------------------------------------------------------------------


def test_realistic_multi_entry_payload() -> None:
    blob = _join(
        b"# branch.oid 1234567",
        b"# branch.head main",
        b"# branch.upstream origin/main",
        b"# branch.ab +1 -0",
        _ordinary(".M", "src/foo.py"),
        _ordinary("M.", "src/bar.py"),
        _rename("R.", "renamed.py", "original.py"),
        _untracked("brand_new.txt"),
        _unmerged("UU", "conflict.py"),
    )
    result = parse_porcelain_v2(blob)
    assert set(result.keys()) == {
        "src/foo.py",
        "src/bar.py",
        "renamed.py",
        "brand_new.txt",
        "conflict.py",
    }
    assert result["src/foo.py"].state == STATE_UNSTAGED
    assert result["src/bar.py"].state == STATE_STAGED
    assert result["renamed.py"].state == STATE_RENAMED
    assert result["brand_new.txt"].state == STATE_UNTRACKED
    assert result["conflict.py"].state == STATE_CONFLICTED


# ---------------------------------------------------------------------------
# Robustness — malformed input must NOT crash the watcher loop.
# ---------------------------------------------------------------------------


def test_truncated_ordinary_record_skipped() -> None:
    # "1 .M foo" is missing fields — parser must skip, not raise.
    blob = b"1 .M foo\x00" + _ordinary(".M", "good.py") + b"\x00"
    result = parse_porcelain_v2(blob)
    assert "good.py" in result
    assert "foo" not in result


def test_unknown_prefix_dropped() -> None:
    # A hypothetical future record type (e.g. "x ...") should be ignored,
    # not crash. Forward-compat over strictness.
    blob = b"x some new thing\x00" + _ordinary(".M", "good.py") + b"\x00"
    assert "good.py" in parse_porcelain_v2(blob)


def test_rename_missing_orig_path_does_not_consume_next_record() -> None:
    # If a `2` record arrives without its companion origPath field (e.g.
    # truncated mid-write), we must NOT silently swallow the next record
    # as the orig_path. The parser skips the malformed rename cleanly.
    rename_head = b"2 R. N... 100644 100644 100644 abc def R100 renamed.py"
    blob = rename_head + b"\x00"  # no origPath companion
    result = parse_porcelain_v2(blob)
    # Either: the rename is skipped (acceptable), OR: it's parsed with
    # whatever the next field happens to be (NOT acceptable). We assert
    # the safe behaviour: no entry recorded for the malformed rename.
    assert "renamed.py" not in result


def test_unknown_status_char_uses_fallback_tooltip() -> None:
    # If git emits a status combination we haven't catalogued (e.g. a new
    # subtype), the parser must still produce a usable badge — falling
    # back to a generic tooltip rather than raising KeyError.
    blob = _join(_ordinary(".X", "weird.py"))
    status = parse_porcelain_v2(blob)["weird.py"]
    assert status.char == "X"
    assert status.state == STATE_UNSTAGED
    assert "X" in status.tooltip  # fallback shape: "unstaged (X)"


# ---------------------------------------------------------------------------
# GitStatus value-type discipline
# ---------------------------------------------------------------------------


def test_gitstatus_is_frozen() -> None:
    # `frozen=True` is the contract that lets us pass GitStatus values
    # across signal boundaries without worrying about downstream mutation.
    status = GitStatus(path="x", char="M", state=STATE_UNSTAGED, tooltip="Modified")
    try:
        status.path = "y"  # type: ignore[misc]
    except (
        Exception
    ) as exc:  # FrozenInstanceError or AttributeError depending on Python version
        assert (
            "frozen" in str(exc).lower()
            or "can't set" in str(exc).lower()
            or "cannot" in str(exc).lower()
        )
    else:
        raise AssertionError("GitStatus must be frozen — assignment should have raised")


# ---------------------------------------------------------------------------
# Directory aggregation — ancestor synthesis for changed paths.
# ---------------------------------------------------------------------------


def _make(path: str, state: str = STATE_UNSTAGED, char: str = "M") -> GitStatus:
    return GitStatus(path=path, char=char, state=state, tooltip="x")


def test_aggregate_empty_map_returns_empty() -> None:
    assert _add_directory_aggregates({}) == {}


def test_aggregate_root_level_file_has_no_ancestors() -> None:
    # "foo.py" has zero strict ancestors — no aggregate entry created.
    file_map = {"foo.py": _make("foo.py")}
    result = _add_directory_aggregates(file_map)
    assert set(result.keys()) == {"foo.py"}


def test_aggregate_single_nested_file_creates_one_ancestor() -> None:
    file_map = {"src/foo.py": _make("src/foo.py")}
    result = _add_directory_aggregates(file_map)
    assert set(result.keys()) == {"src/foo.py", "src"}
    aggregate = result["src"]
    assert aggregate.char == "·"
    assert aggregate.state == STATE_UNSTAGED
    assert aggregate.tooltip == "1 file changed"


def test_aggregate_deeply_nested_file_creates_every_ancestor() -> None:
    file_map = {"a/b/c/d.py": _make("a/b/c/d.py")}
    result = _add_directory_aggregates(file_map)
    assert set(result.keys()) == {"a/b/c/d.py", "a/b/c", "a/b", "a"}
    for ancestor in ("a", "a/b", "a/b/c"):
        assert result[ancestor].char == "·"
        assert result[ancestor].state == STATE_UNSTAGED


def test_aggregate_multiple_files_in_same_dir_counts_correctly() -> None:
    file_map = {
        "src/foo.py": _make("src/foo.py"),
        "src/bar.py": _make("src/bar.py"),
        "src/baz.py": _make("src/baz.py"),
    }
    result = _add_directory_aggregates(file_map)
    assert result["src"].tooltip == "3 files changed"


def test_aggregate_dominant_state_priority() -> None:
    # Mix states under one ancestor; aggregate must pick the highest priority.
    # Priority order: conflicted > unstaged > staged > renamed > untracked.
    file_map = {
        "src/clean_staged.py": _make("src/clean_staged.py", state=STATE_STAGED),
        "src/untracked.txt": _make("src/untracked.txt", state=STATE_UNTRACKED),
        "src/conflict.py": _make("src/conflict.py", state=STATE_CONFLICTED, char="U"),
    }
    result = _add_directory_aggregates(file_map)
    assert result["src"].state == STATE_CONFLICTED


def test_aggregate_unstaged_beats_staged() -> None:
    file_map = {
        "src/a.py": _make("src/a.py", state=STATE_STAGED),
        "src/b.py": _make("src/b.py", state=STATE_UNSTAGED),
    }
    result = _add_directory_aggregates(file_map)
    assert result["src"].state == STATE_UNSTAGED


def test_aggregate_shared_ancestor_count_sums_across_branches() -> None:
    # "a/b/x.py" and "a/c/y.py" share ancestor "a" but have separate "b" and
    # "c" ancestors. "a" should see both files in its count.
    file_map = {
        "a/b/x.py": _make("a/b/x.py"),
        "a/c/y.py": _make("a/c/y.py"),
    }
    result = _add_directory_aggregates(file_map)
    assert result["a"].tooltip == "2 files changed"
    assert result["a/b"].tooltip == "1 file changed"
    assert result["a/c"].tooltip == "1 file changed"


# ---------------------------------------------------------------------------
# GitController — Qt facade smoke tests (no subprocess, no real repo).
# ---------------------------------------------------------------------------
#
# The conftest.py session fixture provides a QCoreApplication, which is what
# GitController needs (QFileSystemWatcher + QTimer require an event loop's
# Qt environment to be initialised). We don't drive the worker through real
# subprocess calls here — instead we instantiate the controller, optionally
# mutate its private state directly to simulate a completed scan, and assert
# the QML-facing API behaves correctly. Subprocess paths are covered by the
# pure-parse tests above; the Qt facade is just glue.


def test_controller_constructs_and_stops_cleanly() -> None:
    controller = GitController()
    assert controller.repoRoot == ""
    controller.stop()
    # After stop(), the worker should have exited (best-effort join, ≤1s).
    assert not controller._worker.is_alive()


def test_working_tree_changed_fires_on_debounce_timeout() -> None:
    """The debounce timer's timeout must re-emit the public
    `workingTreeChanged` edge that AppController routes to
    NvimBackend.checktime(). Guards the signal-to-signal wire only;
    `test_poke_funnels_through_debounce` covers that change paths actually
    reach the debounce. Emit the timeout directly rather than wait out the
    real 200ms single-shot timer — we're guarding the wire, not the QTimer."""
    controller = GitController()
    try:
        received: list = []
        controller.workingTreeChanged.connect(lambda: received.append(True))
        controller._debounce.timeout.emit()
        assert received == [True]
    finally:
        controller.stop()


def test_poke_funnels_through_debounce() -> None:
    """`poke()` (the `gitpoke` editor-save path) routes through the shared
    `_debounce`, which is what couples it to the `workingTreeChanged` edge
    via the timeout wire. Backs the "every change path funnels through
    `_debounce`" claim for the public poke path. The real 200ms timer
    never fires in-test (no running event loop), so `isActive()` after
    `poke()` is a deterministic check."""
    controller = GitController()
    try:
        assert not controller._debounce.isActive()
        controller.poke()
        assert controller._debounce.isActive()
    finally:
        controller.stop()


def test_controller_status_for_path_returns_empty_when_no_repo() -> None:
    controller = GitController()
    try:
        # No repo root set yet — every lookup returns {}.
        assert controller.statusForPath("/anywhere/foo.py") == {}
    finally:
        controller.stop()


def test_controller_status_for_path_relative_conversion() -> None:
    # Inject state directly to bypass the worker; we're testing the
    # absolute-to-relative-path conversion in statusForPath, not subprocess.
    controller = GitController()
    try:
        with controller._lock:
            controller._resolved_root = "/home/jc/repo"
            controller._status_map = {
                "src/foo.py": GitStatus(
                    path="src/foo.py",
                    char="M",
                    state=STATE_UNSTAGED,
                    tooltip="Modified",
                ),
            }
        result = controller.statusForPath("/home/jc/repo/src/foo.py")
        # `additions`/`deletions` default to 0 on GitStatus and are
        # always present in the return dict so the QML adapter can
        # forward them as `adds`/`dels` without an absent-vs-zero check.
        # `path` is the resolved-root-relative path (= GitStatusListModel's
        # `displayName` role) — the git viewer's changes tree keys its
        # working-diff request on it, so it must be relative to the resolved
        # toplevel (the `git diff` cwd), not the asked `repoRoot`.
        assert result == {
            "char": "M",
            "state": STATE_UNSTAGED,
            "tooltip": "Modified",
            "path": "src/foo.py",
            "additions": 0,
            "deletions": 0,
        }
    finally:
        controller.stop()


def test_controller_status_for_path_outside_repo_returns_empty() -> None:
    controller = GitController()
    try:
        with controller._lock:
            controller._resolved_root = "/home/jc/repo"
            controller._status_map = {
                "src/foo.py": GitStatus(
                    path="src/foo.py",
                    char="M",
                    state=STATE_UNSTAGED,
                    tooltip="Modified",
                ),
            }
        # Path is outside the repo root — empty result, no exception.
        assert controller.statusForPath("/tmp/elsewhere/foo.py") == {}
    finally:
        controller.stop()


def test_controller_status_for_path_clean_file_returns_empty() -> None:
    controller = GitController()
    try:
        with controller._lock:
            controller._resolved_root = "/home/jc/repo"
            controller._status_map = {}  # clean tree
        assert controller.statusForPath("/home/jc/repo/src/foo.py") == {}
    finally:
        controller.stop()


def test_controller_status_for_path_includes_orig_path_for_renames() -> None:
    controller = GitController()
    try:
        with controller._lock:
            controller._resolved_root = "/home/jc/repo"
            controller._status_map = {
                "new.py": GitStatus(
                    path="new.py",
                    char="R",
                    state=STATE_RENAMED,
                    tooltip="Renamed",
                    orig_path="old.py",
                ),
            }
        result = controller.statusForPath("/home/jc/repo/new.py")
        assert result["origPath"] == "old.py"
        # additions/deletions are always present since this commit — verify
        # the rename path includes them at their default-zero values.
        assert result["additions"] == 0
        assert result["deletions"] == 0
    finally:
        controller.stop()


def test_controller_set_repo_root_clears_existing_state() -> None:
    controller = GitController()
    try:
        with controller._lock:
            controller._resolved_root = "/home/jc/old_repo"
            controller._status_map = {
                "foo.py": GitStatus(
                    path="foo.py",
                    char="M",
                    state=STATE_UNSTAGED,
                    tooltip="Modified",
                ),
            }
        # Switching to a new repo must clear the old map synchronously, so
        # the UI doesn't briefly show the previous project's badges.
        controller.set_repo_root("/home/jc/new_repo")
        assert controller.statusForPath("/home/jc/old_repo/foo.py") == {}
        with controller._lock:
            assert controller._status_map == {}
            assert controller._resolved_root == ""
    finally:
        controller.stop()


def test_controller_set_repo_root_is_idempotent_on_equal_value() -> None:
    controller = GitController()
    received: list[None] = []
    controller.repoRootChanged.connect(lambda: received.append(None))
    try:
        controller.set_repo_root("/some/path")
        controller.set_repo_root("/some/path")  # same value — no-op
        # Exactly one repoRootChanged emission.
        assert len(received) == 1
    finally:
        controller.stop()


def test_controller_repo_root_property_reflects_set_value() -> None:
    controller = GitController()
    try:
        controller.set_repo_root("/foo/bar")
        assert controller.repoRoot == "/foo/bar"
    finally:
        controller.stop()


# ---------------------------------------------------------------------------
# file_entries — sorted absolute-path projection for the panel.
# ---------------------------------------------------------------------------


def _inject(controller: GitController, resolved: str, *entries: GitStatus) -> None:
    """Direct-set the controller's internal state. Test-only intimacy."""
    with controller._lock:
        controller._resolved_root = resolved
        controller._status_map = {e.path: e for e in entries}


def test_file_entries_empty_when_no_repo() -> None:
    controller = GitController()
    try:
        assert controller._file_entries() == []
    finally:
        controller.stop()


def test_file_entries_returns_absolute_paths() -> None:
    controller = GitController()
    try:
        _inject(
            controller,
            "/home/jc/repo",
            GitStatus(
                path="src/foo.py", char="M", state=STATE_UNSTAGED, tooltip="Modified"
            ),
        )
        entries = controller._file_entries()
        assert len(entries) == 1
        abs_path, status = entries[0]
        assert abs_path == "/home/jc/repo/src/foo.py"
        assert status.path == "src/foo.py"
    finally:
        controller.stop()


def test_file_entries_filters_directory_aggregates() -> None:
    # `_add_directory_aggregates` populates entries with char='·' for every
    # ancestor; the panel must not see them.
    controller = GitController()
    try:
        _inject(
            controller,
            "/repo",
            GitStatus(
                path="src/foo.py", char="M", state=STATE_UNSTAGED, tooltip="Modified"
            ),
            GitStatus(
                path="src", char="·", state=STATE_UNSTAGED, tooltip="1 file changed"
            ),
        )
        entries = controller._file_entries()
        assert len(entries) == 1
        assert entries[0][1].path == "src/foo.py"
    finally:
        controller.stop()


def test_file_entries_sorted_by_repo_relative_path() -> None:
    controller = GitController()
    try:
        _inject(
            controller,
            "/repo",
            GitStatus(
                path="zeta.py", char="M", state=STATE_UNSTAGED, tooltip="Modified"
            ),
            GitStatus(
                path="alpha.py", char="M", state=STATE_UNSTAGED, tooltip="Modified"
            ),
            GitStatus(
                path="middle.py", char="M", state=STATE_UNSTAGED, tooltip="Modified"
            ),
        )
        entries = controller._file_entries()
        assert [e[1].path for e in entries] == ["alpha.py", "middle.py", "zeta.py"]
    finally:
        controller.stop()


# ---------------------------------------------------------------------------
# GitStatusListModel — flat projection consumed by the Active Changes panel.
# ---------------------------------------------------------------------------


def test_list_model_starts_empty() -> None:
    controller = GitController()
    try:
        model = GitStatusListModel(controller)
        assert model.rowCount(QModelIndex()) == 0
        assert model.count == 0
    finally:
        controller.stop()


def test_list_model_refresh_populates_from_controller() -> None:
    controller = GitController()
    try:
        model = GitStatusListModel(controller)
        _inject(
            controller,
            "/repo",
            GitStatus(
                path="src/foo.py", char="M", state=STATE_UNSTAGED, tooltip="Modified"
            ),
            GitStatus(
                path="new.txt", char="?", state=STATE_UNTRACKED, tooltip="Untracked"
            ),
        )
        # `_refresh` is the slot the queued connection would call —
        # invoke directly to bypass the worker thread.
        model._refresh()
        assert model.rowCount(QModelIndex()) == 2
        assert model.count == 2
    finally:
        controller.stop()


def test_list_model_roles_expose_all_fields() -> None:
    controller = GitController()
    try:
        model = GitStatusListModel(controller)
        _inject(
            controller,
            "/repo",
            GitStatus(
                path="src/foo.py", char="M", state=STATE_UNSTAGED, tooltip="Modified"
            ),
        )
        model._refresh()
        idx = model.index(0, 0)
        assert model.data(idx, GitStatusListModel.PathRole) == "/repo/src/foo.py"
        assert model.data(idx, GitStatusListModel.DisplayNameRole) == "src/foo.py"
        assert model.data(idx, GitStatusListModel.CharRole) == "M"
        assert model.data(idx, GitStatusListModel.StateRole) == STATE_UNSTAGED
        assert model.data(idx, GitStatusListModel.TooltipRole) == "Modified"
    finally:
        controller.stop()


def test_list_model_filters_directory_aggregates() -> None:
    # Directory aggregates (char='·') must NOT appear in the panel — they
    # belong on the file tree only. file_entries() drops them, so the model
    # should never see them.
    controller = GitController()
    try:
        model = GitStatusListModel(controller)
        _inject(
            controller,
            "/repo",
            GitStatus(
                path="src/foo.py", char="M", state=STATE_UNSTAGED, tooltip="Modified"
            ),
            GitStatus(
                path="src", char="·", state=STATE_UNSTAGED, tooltip="1 file changed"
            ),
        )
        model._refresh()
        assert model.rowCount(QModelIndex()) == 1
        assert (
            model.data(model.index(0, 0), GitStatusListModel.DisplayNameRole)
            == "src/foo.py"
        )
    finally:
        controller.stop()


def test_list_model_count_changed_emitted_on_size_change() -> None:
    controller = GitController()
    try:
        model = GitStatusListModel(controller)
        received: list[None] = []
        model.countChanged.connect(lambda: received.append(None))
        _inject(
            controller,
            "/repo",
            GitStatus(path="a.py", char="M", state=STATE_UNSTAGED, tooltip="Modified"),
        )
        model._refresh()
        assert len(received) == 1
        # Same data again — count unchanged, no emit.
        model._refresh()
        assert len(received) == 1
        # Add a second file — count goes 1→2, emit fires once.
        _inject(
            controller,
            "/repo",
            GitStatus(path="a.py", char="M", state=STATE_UNSTAGED, tooltip="Modified"),
            GitStatus(
                path="b.py", char="?", state=STATE_UNTRACKED, tooltip="Untracked"
            ),
        )
        model._refresh()
        assert len(received) == 2
    finally:
        controller.stop()


def test_list_model_role_names_use_kebab_qml_form() -> None:
    # Sanity-check role names land as bytes (Qt convention) and match the
    # QML property names the delegate references.
    controller = GitController()
    try:
        model = GitStatusListModel(controller)
        names = model.roleNames()
        assert names[GitStatusListModel.PathRole] == b"path"
        assert names[GitStatusListModel.DisplayNameRole] == b"displayName"
        assert names[GitStatusListModel.CharRole] == b"statusChar"
        assert names[GitStatusListModel.StateRole] == b"statusState"
        assert names[GitStatusListModel.TooltipRole] == b"tooltip"
    finally:
        controller.stop()


# ---------------------------------------------------------------------------
# _publish — signal suppression on equal map
# ---------------------------------------------------------------------------


def test_publish_suppresses_signal_on_equal_map() -> None:
    # When the newly-scanned map is identical to the previous one,
    # _publish must NOT emit statusChanged — a spurious modelReset would
    # invalidate every visible delegate binding for no real change.
    # Common scenario: an unrelated .git/ file changes (fsmonitor cache
    # touch) and git status produces the same output as before.
    controller = GitController()
    try:
        status = GitStatus(
            path="src/foo.py", char="M", state=STATE_UNSTAGED, tooltip="Modified"
        )
        with controller._lock:
            controller._status_map = {"src/foo.py": status}
            controller._resolved_root = "/repo"
        received: list[None] = []
        controller.statusChanged.connect(lambda: received.append(None))
        # Publish the SAME map + same stats — should be a no-op.
        controller._publish({"src/foo.py": status}, "/repo", GitStats(), {})
        assert len(received) == 0, "statusChanged must not fire when map is unchanged"
        # Publish a DIFFERENT map — should fire.
        controller._publish({}, "", GitStats(), {})
        assert len(received) == 1, "statusChanged must fire when map changes"
    finally:
        controller.stop()


# ---------------------------------------------------------------------------
# set_repo_root empty string — clears state and re-emits
# ---------------------------------------------------------------------------


def test_controller_set_repo_root_to_empty_clears_state() -> None:
    # Setting repoRoot to "" after it was non-empty should synchronously
    # clear the map and resolved root, emit statusChanged (so the panel
    # hides), and emit repoRootChanged.
    controller = GitController()
    try:
        with controller._lock:
            controller._resolved_root = "/home/jc/repo"
            controller._status_map = {
                "foo.py": GitStatus(
                    path="foo.py",
                    char="M",
                    state=STATE_UNSTAGED,
                    tooltip="Modified",
                ),
            }
        controller._repo_root = "/home/jc/repo"  # prime the guard for idempotency

        root_changes: list[None] = []
        status_changes: list[None] = []
        controller.repoRootChanged.connect(lambda: root_changes.append(None))
        controller.statusChanged.connect(lambda: status_changes.append(None))

        controller.set_repo_root("")

        assert controller.repoRoot == ""
        with controller._lock:
            assert controller._status_map == {}
            assert controller._resolved_root == ""
        assert len(root_changes) == 1, "repoRootChanged must fire on root change"
        assert len(status_changes) == 1, "statusChanged must fire to clear the panel"
    finally:
        controller.stop()


# ---------------------------------------------------------------------------
# parse_numstat_blob — `git diff --numstat -z` parser.
# ---------------------------------------------------------------------------


def _numstat(adds: str, dels: str, path: str) -> bytes:
    """One ordinary numstat row: ``adds\\tdels\\tpath``."""
    return f"{adds}\t{dels}\t{path}".encode("utf-8")


def _numstat_rename(adds: str, dels: str, new: str) -> bytes:
    """One rename row with `-z`: empty path slot, then a second NUL field."""
    head = f"{adds}\t{dels}\t".encode("utf-8")  # trailing tab, empty path
    return head + b"\x00" + new.encode("utf-8")


def test_numstat_empty_blob_returns_empty_map() -> None:
    assert parse_numstat_blob(b"") == {}


def test_numstat_parses_ordinary_entries() -> None:
    blob = (
        b"\x00".join(
            [_numstat("12", "3", "src/foo.py"), _numstat("0", "8", "src/bar.py")]
        )
        + b"\x00"
    )
    assert parse_numstat_blob(blob) == {
        "src/foo.py": (12, 3),
        "src/bar.py": (0, 8),
    }


def test_numstat_skips_binary_rows() -> None:
    # Binary files are emitted with `-` for both counts. Skip them — the
    # panel can't render `+? -?` and the bash reference does the same.
    blob = (
        b"\x00".join(
            [_numstat("-", "-", "assets/logo.png"), _numstat("4", "1", "src/foo.py")]
        )
        + b"\x00"
    )
    assert parse_numstat_blob(blob) == {"src/foo.py": (4, 1)}


def test_numstat_handles_renames_keys_on_new_path() -> None:
    # Renames in -z mode put the NEW path in a second NUL-terminated field.
    # We key on the new path because the porcelain status row's path is
    # also the new path — keeps the merge step's lookup trivial.
    blob = _numstat_rename("5", "2", "new/a.py") + b"\x00"
    assert parse_numstat_blob(blob) == {"new/a.py": (5, 2)}


def test_numstat_malformed_rows_dropped_silently() -> None:
    # Rows missing the third tab-separated field would crash a strict
    # parser; we drop them so a partial pipe read can't kill the worker.
    blob = b"only_one\x00" + _numstat("1", "0", "src/foo.py") + b"\x00"
    assert parse_numstat_blob(blob) == {"src/foo.py": (1, 0)}


def test_numstat_non_integer_counts_dropped() -> None:
    # Defensive: a future git version emitting non-numeric values in the
    # count columns shouldn't crash. (Today only `-` does that, but a
    # value-error skip is cheap insurance.)
    blob = (
        b"\x00".join([_numstat("abc", "1", "src/x.py"), _numstat("1", "0", "src/y.py")])
        + b"\x00"
    )
    assert parse_numstat_blob(blob) == {"src/y.py": (1, 0)}


def test_numstat_binary_rename_skips_and_realigns() -> None:
    # The defensive branch in parse_numstat_blob: when a binary row (-/-) has
    # an empty path slot (i.e. it's a rename with binary marker), the iterator
    # must consume the second NUL-terminated field so it realigns on the next
    # ordinary entry. Without the `i += 1` guard, "other.py" would be
    # misinterpreted as the continuation of a normal row.
    blob = b"-\t-\t\x00new/a.py\x00" + b"3\t1\tother.py\x00"
    result = parse_numstat_blob(blob)
    assert "new/a.py" not in result, "binary rename must be dropped"
    assert result == {"other.py": (3, 1)}, (
        "entry after binary rename must parse correctly"
    )


# ---------------------------------------------------------------------------
# _merge_numstat_into_map — fold numstat data into the GitStatus map.
# ---------------------------------------------------------------------------


def test_merge_populates_unstaged_from_unstaged_numstat() -> None:
    file_map = {
        "src/foo.py": GitStatus(
            path="src/foo.py", char="M", state=STATE_UNSTAGED, tooltip="Modified"
        )
    }
    merged = _merge_numstat_into_map(file_map, {}, {"src/foo.py": (10, 3)}, {})
    assert merged["src/foo.py"].additions == 10
    assert merged["src/foo.py"].deletions == 3


def test_merge_populates_staged_from_staged_numstat() -> None:
    file_map = {
        "src/foo.py": GitStatus(
            path="src/foo.py", char="M", state=STATE_STAGED, tooltip="Modified (staged)"
        )
    }
    merged = _merge_numstat_into_map(file_map, {"src/foo.py": (4, 1)}, {}, {})
    assert merged["src/foo.py"].additions == 4
    assert merged["src/foo.py"].deletions == 1


def test_merge_untracked_uses_line_count_as_additions() -> None:
    # Untracked files don't have a diff — they're new in their entirety.
    # We surface the file's line count as `additions` so the row reads
    # `+N`, mirroring how the reference statusline shows untracked work.
    file_map = {
        "new.py": GitStatus(
            path="new.py", char="?", state=STATE_UNTRACKED, tooltip="Untracked"
        )
    }
    merged = _merge_numstat_into_map(file_map, {}, {}, {"new.py": 42})
    assert merged["new.py"].additions == 42
    assert merged["new.py"].deletions == 0


def test_merge_renamed_uses_staged_numstat_first() -> None:
    # Rename rows in porcelain v2 live on the staged side (X=R, Y=.), so
    # the matching numstat entry is in the staged map.
    file_map = {
        "new/a.py": GitStatus(
            path="new/a.py",
            char="R",
            state=STATE_RENAMED,
            tooltip="Renamed",
            orig_path="old/a.py",
        )
    }
    merged = _merge_numstat_into_map(file_map, {"new/a.py": (3, 2)}, {}, {})
    assert merged["new/a.py"].additions == 3
    assert merged["new/a.py"].deletions == 2


def test_merge_unmatched_entry_defaults_to_zero() -> None:
    # A file in the status map but missing from numstat (binary, rename
    # arrow mismatch, etc.) keeps the dataclass defaults so the row's
    # delta column hides itself.
    file_map = {
        "src/foo.py": GitStatus(
            path="src/foo.py", char="M", state=STATE_UNSTAGED, tooltip="Modified"
        )
    }
    merged = _merge_numstat_into_map(file_map, {}, {}, {})
    assert merged["src/foo.py"].additions == 0
    assert merged["src/foo.py"].deletions == 0


def test_merge_ignored_files_get_zero_delta() -> None:
    file_map = {
        "build/out.o": GitStatus(
            path="build/out.o", char="!", state=STATE_IGNORED, tooltip="Ignored"
        )
    }
    # Even if a numstat entry exists (it shouldn't for ignored files,
    # but defensively), the merge selects 0/0 for STATE_IGNORED.
    merged = _merge_numstat_into_map(file_map, {"build/out.o": (5, 5)}, {}, {})
    assert merged["build/out.o"].additions == 0
    assert merged["build/out.o"].deletions == 0


def test_merge_conflicted_uses_staged_or_unstaged() -> None:
    # Conflicted files live in unmerged state; numstat may show them on
    # either side depending on which side the user has touched. We try
    # staged first, then unstaged, so the row carries SOMETHING when
    # data is available.
    file_map = {
        "src/x.py": GitStatus(
            path="src/x.py", char="U", state=STATE_CONFLICTED, tooltip="Conflicted"
        )
    }
    merged = _merge_numstat_into_map(file_map, {}, {"src/x.py": (7, 4)}, {})
    assert merged["src/x.py"].additions == 7
    assert merged["src/x.py"].deletions == 4


# ---------------------------------------------------------------------------
# _compute_stats — header bucket aggregation.
# ---------------------------------------------------------------------------


def test_compute_stats_sums_per_bucket() -> None:
    stats = _compute_stats(
        staged_ns={"a.py": (10, 3), "b.py": (2, 0)},
        unstaged_ns={"c.py": (5, 1)},
        untracked_lc={"d.py": 8, "e.py": 12},
        untracked_total=2,
    )
    assert stats.staged_add == 12
    assert stats.staged_del == 3
    assert stats.staged_files == 2
    assert stats.unstaged_add == 5
    assert stats.unstaged_del == 1
    assert stats.unstaged_files == 1
    assert stats.untracked_lines == 20
    assert stats.untracked_count == 2


def test_compute_stats_double_counts_mm_files() -> None:
    # A doubly-modified ("MM") file appears in both numstat dicts and is
    # counted in both buckets — accurate, because it has real work on
    # both sides. The panel renders only one row for it (worktree
    # precedence) but the header is meant to answer per-side.
    stats = _compute_stats(
        staged_ns={"foo.py": (3, 0)},
        unstaged_ns={"foo.py": (1, 5)},
        untracked_lc={},
        untracked_total=0,
    )
    assert stats.staged_files == 1
    assert stats.unstaged_files == 1
    assert stats.staged_add == 3
    assert stats.unstaged_del == 5


def test_compute_stats_untracked_total_separate_from_lines() -> None:
    # When the line-count map is partial (cap hit), `untracked_count`
    # still reflects the full count.
    stats = _compute_stats(
        staged_ns={},
        unstaged_ns={},
        untracked_lc={"a.py": 3, "b.py": 5},  # only 2 sampled
        untracked_total=100,  # but 100 actual untracked files
    )
    assert stats.untracked_lines == 8
    assert stats.untracked_count == 100


def test_compute_stats_empty_returns_default() -> None:
    assert _compute_stats({}, {}, {}, 0) == GitStats()


# ---------------------------------------------------------------------------
# _count_untracked_lines — Python file-read pass with binary skip + cap.
# ---------------------------------------------------------------------------


def test_count_untracked_lines_counts_newlines(tmp_path) -> None:
    from symmetria_ide.git_controller import GitController

    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n")
    (tmp_path / "b.txt").write_text("hi\n")
    controller = GitController()
    try:
        result = controller._count_untracked_lines(str(tmp_path), ["a.txt", "b.txt"])
        assert result == {"a.txt": 3, "b.txt": 1}
    finally:
        controller.stop()


def test_count_untracked_lines_skips_binary_files(tmp_path) -> None:
    from symmetria_ide.git_controller import GitController

    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00binary")
    (tmp_path / "a.txt").write_text("line1\nline2\n")
    controller = GitController()
    try:
        result = controller._count_untracked_lines(str(tmp_path), ["logo.png", "a.txt"])
        # Binary file dropped, text file present with correct count.
        assert "logo.png" not in result
        assert result["a.txt"] == 2
    finally:
        controller.stop()


def test_count_untracked_lines_respects_cap(tmp_path) -> None:
    from symmetria_ide.git_controller import GitController

    # Twenty-five untracked text files, cap at 5.
    paths: list[str] = []
    for i in range(25):
        name = f"f{i:02d}.txt"
        (tmp_path / name).write_text(f"{i}\n")
        paths.append(name)
    controller = GitController()
    try:
        result = controller._count_untracked_lines(str(tmp_path), paths, cap=5)
        assert len(result) == 5
    finally:
        controller.stop()


def test_count_untracked_lines_missing_file_silently_dropped(tmp_path) -> None:
    from symmetria_ide.git_controller import GitController

    (tmp_path / "real.txt").write_text("a\nb\n")
    controller = GitController()
    try:
        result = controller._count_untracked_lines(
            str(tmp_path), ["real.txt", "ghost.txt"]
        )
        assert result == {"real.txt": 2}
    finally:
        controller.stop()


def test_count_untracked_lines_empty_paths_returns_empty() -> None:
    from symmetria_ide.git_controller import GitController

    controller = GitController()
    try:
        assert controller._count_untracked_lines("/tmp", []) == {}
    finally:
        controller.stop()


# ---------------------------------------------------------------------------
# GitStatusListModel — new additions/deletions roles.
# ---------------------------------------------------------------------------


def test_list_model_exposes_additions_deletions_roles() -> None:
    controller = GitController()
    try:
        model = GitStatusListModel(controller)
        _inject(
            controller,
            "/repo",
            GitStatus(
                path="src/foo.py",
                char="M",
                state=STATE_UNSTAGED,
                tooltip="Modified",
                additions=12,
                deletions=5,
            ),
        )
        model._refresh()
        idx = model.index(0, 0)
        assert model.data(idx, GitStatusListModel.AdditionsRole) == 12
        assert model.data(idx, GitStatusListModel.DeletionsRole) == 5
        names = model.roleNames()
        assert names[GitStatusListModel.AdditionsRole] == b"additions"
        assert names[GitStatusListModel.DeletionsRole] == b"deletions"
    finally:
        controller.stop()


# ---------------------------------------------------------------------------
# GitController.stats — QML property serializes GitStats fields correctly.
# ---------------------------------------------------------------------------


def test_controller_stats_property_serializes_fields() -> None:
    controller = GitController()
    try:
        with controller._lock:
            controller._stats = GitStats(
                staged_add=10,
                staged_del=3,
                staged_files=2,
                unstaged_add=5,
                unstaged_del=1,
                unstaged_files=1,
                untracked_lines=42,
                untracked_count=3,
            )
        s = controller.stats
        assert s == {
            "stagedAdd": 10,
            "stagedDel": 3,
            "stagedFiles": 2,
            "unstagedAdd": 5,
            "unstagedDel": 1,
            "unstagedFiles": 1,
            "untrackedLines": 42,
            "untrackedCount": 3,
        }
    finally:
        controller.stop()


def test_controller_stats_default_zero() -> None:
    controller = GitController()
    try:
        s = controller.stats
        assert s["stagedAdd"] == 0
        assert s["unstagedFiles"] == 0
        assert s["untrackedCount"] == 0
    finally:
        controller.stop()


# ---------------------------------------------------------------------------
# changedPathSet — absolute-path membership map driving the embedded
# FileTreeView's `pathFilter` prop on the Active Changes panel.
# ---------------------------------------------------------------------------


def test_controller_changed_path_set_empty_when_no_repo() -> None:
    # No `_resolved_root` => not in a repo => empty filter map.
    # The panel's auto-hide on `model.count > 0` normally suppresses
    # the empty case before it reaches the embedded tree, but the
    # property must still degrade gracefully.
    controller = GitController()
    try:
        assert controller.changedPathSet == {}
    finally:
        controller.stop()


def test_controller_changed_path_set_empty_on_clean_repo() -> None:
    # In a repo but nothing changed — same empty-map contract.
    controller = GitController()
    try:
        with controller._lock:
            controller._resolved_root = "/home/jc/repo"
            controller._status_map = {}
        assert controller.changedPathSet == {}
    finally:
        controller.stop()


def test_controller_changed_path_set_single_nested_file() -> None:
    # One leaf file plus the ancestor directories synthesized by
    # `_add_directory_aggregates`. The fold should produce 4 entries:
    # rootPath + 2 ancestor dirs + 1 leaf file. Mirrors what the
    # production scan emits — we inject the post-aggregate map directly
    # to keep the test scoped to the property's fold logic.
    controller = GitController()
    try:
        with controller._lock:
            controller._resolved_root = "/home/jc/repo"
            controller._status_map = {
                "src/foo/bar.py": GitStatus(
                    path="src/foo/bar.py",
                    char="M",
                    state=STATE_UNSTAGED,
                    tooltip="Modified",
                ),
                # These two ancestor entries are what
                # `_add_directory_aggregates` would have synthesized.
                "src": GitStatus(
                    path="src",
                    char="·",
                    state=STATE_UNSTAGED,
                    tooltip="1 file changed",
                ),
                "src/foo": GitStatus(
                    path="src/foo",
                    char="·",
                    state=STATE_UNSTAGED,
                    tooltip="1 file changed",
                ),
            }
        result = controller.changedPathSet
        assert result == {
            "/home/jc/repo": True,
            "/home/jc/repo/src": True,
            "/home/jc/repo/src/foo": True,
            "/home/jc/repo/src/foo/bar.py": True,
        }
    finally:
        controller.stop()


def test_controller_changed_path_set_root_level_file() -> None:
    # A file at the repo root has no ancestor directory; the map carries
    # exactly two entries: rootPath itself + the leaf.
    controller = GitController()
    try:
        with controller._lock:
            controller._resolved_root = "/home/jc/repo"
            controller._status_map = {
                "README.md": GitStatus(
                    path="README.md",
                    char="M",
                    state=STATE_UNSTAGED,
                    tooltip="Modified",
                ),
            }
        result = controller.changedPathSet
        assert result == {
            "/home/jc/repo": True,
            "/home/jc/repo/README.md": True,
        }
    finally:
        controller.stop()


def test_controller_changed_path_set_all_paths_absolute() -> None:
    # Every key in the returned map must be an absolute path rooted at
    # _resolved_root — the FM's pathFilter compares against
    # FileSystemModel.entries[i].path which is always absolute.
    controller = GitController()
    try:
        with controller._lock:
            controller._resolved_root = "/home/jc/repo"
            controller._status_map = {
                "src/foo.py": _make("src/foo.py"),
                "src": _make("src", char="·"),
                "tests/test_x.py": _make("tests/test_x.py"),
                "tests": _make("tests", char="·"),
            }
        result = controller.changedPathSet
        for key in result:
            assert key.startswith("/home/jc/repo"), (
                f"non-absolute key in pathFilter map: {key!r}"
            )
        # Sanity: the count matches root + 4 source-map entries.
        assert len(result) == 5
    finally:
        controller.stop()


def test_publish_emits_stats_changed_on_stats_change() -> None:
    # Even when the map is unchanged, stats changing must emit
    # statsChanged so the header re-binds (e.g. user changes the
    # working-tree diff size without changing the file SET).
    controller = GitController()
    try:
        status = GitStatus(
            path="src/foo.py",
            char="M",
            state=STATE_UNSTAGED,
            tooltip="Modified",
        )
        with controller._lock:
            controller._status_map = {"src/foo.py": status}
            controller._resolved_root = "/repo"
            controller._stats = GitStats(unstaged_add=5)
        status_received: list[None] = []
        stats_received: list[None] = []
        controller.statusChanged.connect(lambda: status_received.append(None))
        controller.statsChanged.connect(lambda: stats_received.append(None))
        # Same map, NEW stats — statsChanged only.
        controller._publish(
            {"src/foo.py": status}, "/repo", GitStats(unstaged_add=10), {}
        )
        assert len(status_received) == 0
        assert len(stats_received) == 1
    finally:
        controller.stop()


def test_publish_suppresses_stats_changed_on_equal_stats() -> None:
    # When _publish is called with the same stats AND the same map, neither
    # statusChanged nor statsChanged should fire — a spurious statsChanged
    # would cause unnecessary QML re-evaluations of the header repeater.
    controller = GitController()
    try:
        initial_stats = GitStats(unstaged_add=5)
        status = GitStatus(
            path="src/foo.py",
            char="M",
            state=STATE_UNSTAGED,
            tooltip="Modified",
        )
        with controller._lock:
            controller._status_map = {"src/foo.py": status}
            controller._resolved_root = "/repo"
            controller._stats = initial_stats
        status_received: list[None] = []
        stats_received: list[None] = []
        controller.statusChanged.connect(lambda: status_received.append(None))
        controller.statsChanged.connect(lambda: stats_received.append(None))
        # Publish the SAME map AND SAME stats — both signals must be suppressed.
        controller._publish(
            {"src/foo.py": status}, "/repo", GitStats(unstaged_add=5), {}
        )
        assert len(status_received) == 0, (
            "statusChanged must not fire when map is unchanged"
        )
        assert len(stats_received) == 0, (
            "statsChanged must not fire when stats are unchanged"
        )
    finally:
        controller.stop()


# ---------------------------------------------------------------------------
# _run_ignored_set — parsing + error handling
# ---------------------------------------------------------------------------


def test_run_ignored_set_parses_nul_delimited_output() -> None:
    """_run_ignored_set maps NUL-delimited paths to absolute membership dict."""
    from unittest.mock import MagicMock, patch

    # Simulate `git ls-files -z` output: two dir entries + one file.
    fake_stdout = b"node_modules/\x00.venv/\x00dist/foo.js\x00"
    proc_mock = MagicMock()
    proc_mock.returncode = 0
    proc_mock.stdout = fake_stdout
    proc_mock.stderr = b""

    controller = GitController()
    try:
        with patch("subprocess.run", return_value=proc_mock):
            result = controller._run_ignored_set("/repo/root")

        # Dir entries: trailing slash stripped, joined with cwd.
        assert result.get("/repo/root/node_modules") is True
        assert result.get("/repo/root/.venv") is True
        # Regular file entry: no trailing slash to strip.
        assert result.get("/repo/root/dist/foo.js") is True
        # Membership dict values must all be True.
        assert all(v is True for v in result.values())
    finally:
        controller.stop()


def test_run_ignored_set_returns_empty_dict_on_subprocess_failure() -> None:
    """_run_ignored_set returns {} when the git subprocess exits non-zero."""
    from unittest.mock import MagicMock, patch

    proc_mock = MagicMock()
    proc_mock.returncode = 128
    proc_mock.stdout = b""
    proc_mock.stderr = b"fatal: not a git repository"

    controller = GitController()
    try:
        with patch("subprocess.run", return_value=proc_mock):
            result = controller._run_ignored_set("/not/a/repo")
        assert result == {}
    finally:
        controller.stop()


def test_run_ignored_set_returns_empty_dict_on_timeout() -> None:
    """_run_ignored_set returns {} when subprocess.run raises TimeoutExpired."""
    import subprocess
    from unittest.mock import patch

    controller = GitController()
    try:
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=10),
        ):
            result = controller._run_ignored_set("/repo")
        assert result == {}
    finally:
        controller.stop()


# ---------------------------------------------------------------------------
# ignoredPathSet property — None vs {} contract
# ---------------------------------------------------------------------------


def test_ignored_path_set_returns_none_when_no_resolved_root() -> None:
    """ignoredPathSet returns None before the first scan completes (no resolved root).

    A falsy None return lets the FM fall back to its per-directory check-ignore
    path. An empty dict {} would be truthy in JS and would suppress that fallback,
    causing nothing to be treated as ignored and over-expanding .venv/node_modules.
    """
    controller = GitController()
    try:
        # Freshly constructed: _resolved_root == "" → must return None.
        assert controller.ignoredPathSet is None
    finally:
        controller.stop()


def test_ignored_path_set_returns_dict_after_scan() -> None:
    """ignoredPathSet returns a copy of _ignored_set once _resolved_root is set."""
    controller = GitController()
    try:
        ignored = {"/repo/node_modules": True, "/repo/.venv": True}
        with controller._lock:
            controller._resolved_root = "/repo"
            controller._ignored_set = ignored
        result = controller.ignoredPathSet
        assert result == ignored
        # Must be a copy, not the internal dict.
        assert result is not controller._ignored_set
    finally:
        controller.stop()


def test_ignored_path_set_returns_empty_dict_for_clean_repo() -> None:
    """ignoredPathSet returns {} (not None) when root is known but nothing is ignored.

    An empty {} is falsy in Python but truthy in JS — the FM should treat it as
    "scan complete, nothing ignored" and skip nothing, rather than fall back to
    the slow per-directory check-ignore path.
    """
    controller = GitController()
    try:
        with controller._lock:
            controller._resolved_root = "/repo"
            controller._ignored_set = {}
        result = controller.ignoredPathSet
        assert result == {}
        assert result is not None
    finally:
        controller.stop()


# ---------------------------------------------------------------------------
# Not-a-repo → repo recovery (the cold-start / opened-before-`git init` bug).
#
# A directory opened BEFORE it became a git repo used to resolve "not a repo"
# once, tear down every watcher, and never re-check — freezing the status
# panel "clean" for the life of the instance. The fix is a self-contained
# recovery path (directory sentinel watch + backed-off re-resolve backstop)
# that needs NO external signal: no nvim capsule, no editor save, only the
# filesystem. These tests drive the worker methods directly for determinism
# (a live event loop would make watcher/timer assertions flaky).
# ---------------------------------------------------------------------------


def test_refresh_watcher_arms_repo_sentinel_when_not_a_repo(tmp_path) -> None:
    """A failed resolve (`resolved == ""`) with a real asked-for directory
    arms the SENTINEL: a directory watch on the root + the backstop timer."""
    controller = GitController()
    try:
        controller._repo_root = str(tmp_path)  # a real dir, not a git repo
        controller._refresh_watcher_for_root("")  # not-a-repo branch
        assert str(tmp_path) in controller._watcher.directories()
        assert controller._sentinel_backstop.isActive()
    finally:
        controller.stop()


def test_refresh_watcher_clears_sentinel_when_repo_resolves(tmp_path) -> None:
    """When the root resolves as a repo, the sentinel dir watch is dropped,
    the `.git` trigger files are watched, and the backstop is cancelled."""
    import subprocess

    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    controller = GitController()
    try:
        controller._repo_root = str(tmp_path)
        controller._refresh_watcher_for_root("")  # arm sentinel first
        assert str(tmp_path) in controller._watcher.directories()
        assert controller._sentinel_backstop.isActive()
        # Now the same root resolves as a repo.
        controller._refresh_watcher_for_root(str(tmp_path))
        assert str(tmp_path) not in controller._watcher.directories()
        assert not controller._sentinel_backstop.isActive()
        # `.git/HEAD` exists right after `git init`, so the .git watcher armed.
        assert any(".git" in f for f in controller._watcher.files())
    finally:
        controller.stop()


def test_watched_dir_changed_only_debounces_when_git_exists(tmp_path) -> None:
    """The sentinel's directoryChanged handler ignores unrelated top-level
    churn (no `.git` yet) and only wakes a re-scan once `.git` appears —
    so scaffolding writes don't fork `git rev-parse` on every file."""
    controller = GitController()
    try:
        d = str(tmp_path)
        assert not controller._debounce.isActive()
        controller._on_watched_dir_changed(d)  # no .git yet
        assert not controller._debounce.isActive()
        (tmp_path / ".git").mkdir()
        controller._on_watched_dir_changed(d)  # .git now present
        assert controller._debounce.isActive()
    finally:
        controller.stop()


def test_sentinel_backstop_backs_off_then_lapses_on_resolve(tmp_path) -> None:
    """The backstop doubles its delay each not-a-repo firing (so a permanent
    non-repo settles to a cheap poll), re-arming is idempotent while active,
    and it stands down once a repo resolves."""
    controller = GitController()
    try:
        controller._repo_root = str(tmp_path)  # has a root, not yet a repo
        controller._ensure_repo_sentinel_backstop()
        assert controller._sentinel_backstop_delay_ms == 1000
        assert controller._sentinel_backstop.isActive()

        # Each firing while still not-a-repo asks the worker to re-scan and
        # backs the delay off.
        controller._scan_wakeup.clear()
        controller._on_sentinel_backstop()
        assert controller._scan_wakeup.is_set()  # worker re-scan requested
        assert controller._sentinel_backstop_delay_ms == 2000
        controller._on_sentinel_backstop()
        assert controller._sentinel_backstop_delay_ms == 4000

        # Re-arming must NOT reset the backed-off delay (defeats backoff).
        controller._ensure_repo_sentinel_backstop()
        assert controller._sentinel_backstop_delay_ms == 4000

        # Once a repo resolves, the next firing lapses without re-arming.
        with controller._lock:
            controller._resolved_root = str(tmp_path)
        controller._scan_wakeup.clear()
        controller._on_sentinel_backstop()
        assert not controller._scan_wakeup.is_set()
        assert controller._sentinel_backstop_delay_ms == 0
    finally:
        controller.stop()


def test_sentinel_backstop_caps_at_max_delay(tmp_path) -> None:
    """Backoff never exceeds the cap, so steady-state polling stays cheap."""
    from symmetria_ide.git_controller import _SENTINEL_BACKSTOP_MAX_MS

    controller = GitController()
    try:
        controller._repo_root = str(tmp_path)
        controller._ensure_repo_sentinel_backstop()
        for _ in range(20):  # far past the cap
            controller._on_sentinel_backstop()
        assert controller._sentinel_backstop_delay_ms == _SENTINEL_BACKSTOP_MAX_MS
    finally:
        controller.stop()


def test_status_recovers_after_git_init_without_any_capsule(tmp_path) -> None:
    """Headline integration test: a directory opened as a NON-repo recovers
    real git status after `git init`, driven purely by the filesystem — no
    nvim capsule, no editor save, no `set_repo_root` re-fire.

    We drive `_do_scan` directly (the worker would, but `set_repo_root`
    would race it) and verify both halves of the recovery: the SYNCHRONOUS
    status-map publish, and the watcher-refresh signal `_do_scan` emits. We
    deliver that signal to `_refresh_watcher_for_root` BY HAND rather than
    pumping the event loop — `QCoreApplication.processEvents()` on the
    shared session app would also run deferred `deleteLater`s from earlier
    QML-heavy test modules, tripping the Python-3.14 GC/Qt teardown SEGV
    (gotcha #10). Hand-delivery exercises the exact payload the real
    QueuedConnection carries, deterministically and without that hazard.
    """
    import os
    import subprocess

    (tmp_path / "file.txt").write_text("hello\n")
    controller = GitController()
    try:
        # Spy on the watcher-refresh emit with a synchronous (direct, same-
        # thread) connection — proves `_do_scan` fires it with the right
        # payload, no event loop needed.
        refresh_payloads: list[str] = []
        controller._watcherRefreshRequested.connect(refresh_payloads.append)

        # Point at the dir WITHOUT waking the worker thread (set_repo_root
        # would, racing our direct _do_scan); drive the scan ourselves.
        controller._repo_root = str(tmp_path)
        controller._do_scan()

        # Not a repo yet: empty status (synchronous), and the refresh emit
        # carried "" — delivering it arms the sentinel.
        assert controller._resolved_root == ""
        assert controller.statusForPath(str(tmp_path / "file.txt")) == {}
        assert refresh_payloads[-1] == ""
        controller._refresh_watcher_for_root(refresh_payloads[-1])
        assert str(tmp_path) in controller._watcher.directories()
        assert controller._sentinel_backstop.isActive()

        # The directory BECOMES a repo, purely on the filesystem.
        subprocess.run(
            ["git", "init"], cwd=str(tmp_path), check=True, capture_output=True
        )
        subprocess.run(
            ["git", "add", "-A"], cwd=str(tmp_path), check=True, capture_output=True
        )

        # Re-scan — what the sentinel/backstop wakes. Still no capsule.
        controller._do_scan()

        # Recovered: real root resolved, status populated (synchronous), and
        # the refresh emit now carries the real root — delivering it stands
        # the sentinel + backstop down and arms the real watcher set.
        assert controller._resolved_root != ""
        result = controller.statusForPath(
            os.path.join(controller._resolved_root, "file.txt")
        )
        assert result != {}
        assert result["path"] == "file.txt"
        assert refresh_payloads[-1] == controller._resolved_root
        controller._refresh_watcher_for_root(refresh_payloads[-1])
        assert str(tmp_path) not in controller._watcher.directories()
        assert not controller._sentinel_backstop.isActive()
    finally:
        controller.stop()
