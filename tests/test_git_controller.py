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
    GitStatus,
    GitStatusListModel,
    _add_directory_aggregates,
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
        assert result == {"char": "M", "state": STATE_UNSTAGED, "tooltip": "Modified"}
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
        # Publish the SAME map — should be a no-op.
        controller._publish({"src/foo.py": status}, "/repo")
        assert len(received) == 0, "statusChanged must not fire when map is unchanged"
        # Publish a DIFFERENT map — should fire.
        controller._publish({}, "")
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
