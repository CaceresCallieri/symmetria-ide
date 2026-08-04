"""Tests for MinimapModel — Phase 1 / Phase 2 of the editor minimap.

The model is a plain QObject (no QML registration via QmlElement, no
QQuickItem dependency), so unlike `tests/test_minimap_view.py` we can
exercise it at runtime — instantiate, call apply(), assert state and
signal emissions. The QGuiApplication-free `qt_app` fixture is enough.

Coverage targets the PRD §5.4 risk list:
  - R1.2 — invalid payload / wrong types must not crash the GUI thread
  - R1.3 — rapid successive patches keep the splice range correct
  - R2.2 — indent levels are precomputed in apply(), never recomputed
            inside paint() (no str.lstrip per row in the hot path)

Plus the obvious shape contract: snapshot replaces; patch splices;
linesChanged emits with the right range; lineCountChanged emits only
when the count actually changes; indent_level() cache stays parallel
to _lines across both snapshots and patches.
"""

from __future__ import annotations

from symmetria_ide.minimap_model import MinimapModel

# ---------------------------------------------------------------------------
# Empty-state contract
# ---------------------------------------------------------------------------


def test_empty_model_has_zero_count(qt_app):
    """Freshly constructed: zero lines, line_at clamps to ""."""
    del qt_app
    m = MinimapModel()
    assert m.line_count() == 0
    assert m.lineCount == 0
    # Defensive out-of-range — painter loop must not crash if it reads
    # past the count during a transient stale-bookkeeping window.
    assert m.line_at(0) == ""
    assert m.line_at(-1) == ""
    assert m.line_at(100) == ""


# ---------------------------------------------------------------------------
# Snapshot ingest — replaces the buffer
# ---------------------------------------------------------------------------


def test_snapshot_replaces_all_lines(qt_app):
    del qt_app
    m = MinimapModel()
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 3, "lines": ["a", "b", "c"]})
    assert m.line_count() == 3
    assert m.lineCount == 3
    assert m.line_at(0) == "a"
    assert m.line_at(1) == "b"
    assert m.line_at(2) == "c"
    # Past-end still clamps.
    assert m.line_at(3) == ""


def test_snapshot_handles_empty_buffer(qt_app):
    del qt_app
    m = MinimapModel()
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 0, "lines": []})
    assert m.line_count() == 0


def test_snapshot_overwrites_prior_content(qt_app):
    """A second snapshot wipes the first — minimap should reflect the
    LATEST buffer state, never the residue of an earlier one."""
    del qt_app
    m = MinimapModel()
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 5, "lines": list("abcde")})
    m.apply({"op": "snapshot", "bufnr": 2, "line_count": 2, "lines": ["X", "Y"]})
    assert m.line_count() == 2
    assert m.line_at(0) == "X"
    assert m.line_at(1) == "Y"
    # The 'c'/'d'/'e' from the prior buffer must NOT bleed through.
    assert m.line_at(2) == ""


def test_snapshot_decodes_bytes_defensively(qt_app):
    """pynvim returns str by default but a buffer with non-UTF-8 bytes
    could in theory leak bytes through. The painter's str ops (lstrip,
    indexing) would crash on bytes; the apply path must decode."""
    del qt_app
    m = MinimapModel()
    m.apply(
        {
            "op": "snapshot",
            "bufnr": 1,
            "line_count": 2,
            "lines": [b"hello", b"\xff\xfeinvalid"],
        }
    )
    assert m.line_count() == 2
    assert m.line_at(0) == "hello"
    # Invalid UTF-8 falls through `errors="replace"` to a U+FFFD-bearing
    # str — not bytes, not a crash, not an exception.
    assert isinstance(m.line_at(1), str)


# ---------------------------------------------------------------------------
# Signal emission — linesChanged + lineCountChanged
# ---------------------------------------------------------------------------


def _collect_lines_changed(model: MinimapModel) -> list[tuple[int, int]]:
    """Helper: install a connection that records every (first, last)
    range emitted by linesChanged. Direct connection so the recording
    is synchronous with the emit — no event-loop spin needed.
    pytest-qt's qtbot is not installed in this project; this list-based
    helper covers the assertions we need."""
    captured: list[tuple[int, int]] = []
    model.linesChanged.connect(lambda first, last: captured.append((first, last)))
    return captured


def _collect_count_changed(model: MinimapModel) -> list[int]:
    """Helper: record the lineCount value at every notify emission."""
    captured: list[int] = []
    model.lineCountChanged.connect(lambda: captured.append(model.lineCount))
    return captured


def test_snapshot_emits_full_range(qt_app):
    """Snapshot must emit linesChanged covering the entire post-snapshot
    range (0, N) so painters know to repaint every row."""
    del qt_app
    m = MinimapModel()
    captured = _collect_lines_changed(m)
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 4, "lines": list("WXYZ")})
    assert captured == [(0, 4)]


def test_snapshot_to_empty_emits_zero_zero(qt_app):
    """An empty snapshot still emits (0, 0) so listeners can distinguish
    'no event' (no signal at all) from 'now empty' (signal fires with
    a zero range)."""
    del qt_app
    m = MinimapModel()
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 3, "lines": ["a", "b", "c"]})
    captured = _collect_lines_changed(m)  # connect AFTER initial snapshot
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 0, "lines": []})
    assert captured == [(0, 0)]


def test_line_count_changed_fires_only_on_count_change(qt_app):
    """If two consecutive snapshots have the same line count, only the
    first is a real change on the `lineCount` property — the notify
    signal should fire once for the initial change and NOT for the
    second same-count snapshot.

    Spurious lineCountChanged emissions would force QML's binding
    system to re-evaluate every binding that depends on lineCount even
    when nothing actually changed — measurable cost on rapid typing
    where TextChangedI fires per keystroke.
    """
    del qt_app
    m = MinimapModel()
    # First snapshot — count goes 0 → 3, one notify expected.
    initial_counts = _collect_count_changed(m)
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 3, "lines": list("abc")})
    assert initial_counts == [3]
    # Second snapshot — same count, different content, NO notify expected.
    initial_counts.clear()
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 3, "lines": list("xyz")})
    assert initial_counts == [], (
        "lineCountChanged emitted spuriously when count was unchanged "
        "across snapshots — would cause needless QML binding re-eval"
    )


# ---------------------------------------------------------------------------
# Patch ingest — splices into existing buffer
# ---------------------------------------------------------------------------


def test_patch_replaces_range(qt_app):
    """A patch must splice _lines[first:last] = new_lines."""
    del qt_app
    m = MinimapModel()
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 5, "lines": list("abcde")})
    m.apply(
        {
            "op": "patch",
            "bufnr": 1,
            "line_count": 5,
            "first": 1,
            "last": 4,
            "lines": ["B", "C", "D"],
        }
    )
    # _lines[1:4] = ["B","C","D"] → ["a","B","C","D","e"]
    assert [m.line_at(i) for i in range(5)] == ["a", "B", "C", "D", "e"]


def test_patch_pure_insertion(qt_app):
    """first == last with non-empty lines is a pure insertion."""
    del qt_app
    m = MinimapModel()
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 3, "lines": list("abc")})
    m.apply(
        {
            "op": "patch",
            "bufnr": 1,
            "line_count": 5,
            "first": 1,
            "last": 1,
            "lines": ["X", "Y"],
        }
    )
    assert m.line_count() == 5
    assert [m.line_at(i) for i in range(5)] == ["a", "X", "Y", "b", "c"]


def test_patch_pure_deletion(qt_app):
    """Empty lines with first < last is a pure deletion."""
    del qt_app
    m = MinimapModel()
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 5, "lines": list("abcde")})
    m.apply(
        {
            "op": "patch",
            "bufnr": 1,
            "line_count": 3,
            "first": 1,
            "last": 3,
            "lines": [],
        }
    )
    assert m.line_count() == 3
    assert [m.line_at(i) for i in range(3)] == ["a", "d", "e"]


def test_patch_at_row_zero(qt_app):
    """Patch at the first row — common edge case."""
    del qt_app
    m = MinimapModel()
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 3, "lines": list("abc")})
    m.apply(
        {
            "op": "patch",
            "bufnr": 1,
            "line_count": 3,
            "first": 0,
            "last": 1,
            "lines": ["A"],
        }
    )
    assert m.line_at(0) == "A"
    assert m.line_at(1) == "b"


def test_patch_at_last_row(qt_app):
    """Patch at the last row — another common edge case (append-style)."""
    del qt_app
    m = MinimapModel()
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 3, "lines": list("abc")})
    m.apply(
        {
            "op": "patch",
            "bufnr": 1,
            "line_count": 4,
            "first": 3,
            "last": 3,
            "lines": ["d"],
        }
    )
    assert m.line_count() == 4
    assert m.line_at(3) == "d"


def test_rapid_successive_patches_preserve_range(qt_app):
    """R1.3 — rapid TextChangedI events that each splice a different
    range must not drop or corrupt any of them. The model has no
    debouncing of its own; each apply call is independent. This pins
    the contract."""
    del qt_app
    m = MinimapModel()
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 5, "lines": list("abcde")})
    # Three back-to-back patches simulating rapid typing:
    m.apply(
        {
            "op": "patch",
            "bufnr": 1,
            "line_count": 5,
            "first": 0,
            "last": 1,
            "lines": ["A"],
        }
    )
    m.apply(
        {
            "op": "patch",
            "bufnr": 1,
            "line_count": 5,
            "first": 2,
            "last": 3,
            "lines": ["C"],
        }
    )
    m.apply(
        {
            "op": "patch",
            "bufnr": 1,
            "line_count": 5,
            "first": 4,
            "last": 5,
            "lines": ["E"],
        }
    )
    assert [m.line_at(i) for i in range(5)] == ["A", "b", "C", "d", "E"]


def test_patch_emits_affected_range(qt_app):
    """linesChanged from a patch should cover the affected region —
    including past `last` if the splice extends the buffer."""
    del qt_app
    m = MinimapModel()
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 3, "lines": list("abc")})
    # Insert 3 lines at position 1 — splice extends to row 4.
    captured = _collect_lines_changed(m)  # connect AFTER snapshot
    m.apply(
        {
            "op": "patch",
            "bufnr": 1,
            "line_count": 6,
            "first": 1,
            "last": 1,
            "lines": ["X", "Y", "Z"],
        }
    )
    # affected_end = first + len(new) = 1 + 3 = 4; emit max(last=1, 4) = 4.
    assert captured == [(1, 4)]


# ---------------------------------------------------------------------------
# Resilience — bad payloads don't crash the GUI thread (R1.2)
# ---------------------------------------------------------------------------


def test_unknown_op_logged_not_crashed(qt_app):
    """An envelope with op not in {snapshot, patch} must log + drop, not
    propagate. Future nvim versions might introduce new ops; the model
    should ignore them rather than crash."""
    del qt_app
    m = MinimapModel()
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 1, "lines": ["a"]})
    m.apply({"op": "ufo-landing", "bufnr": 1})  # nonsense op
    # State unchanged — the bad envelope was dropped, not applied.
    assert m.line_count() == 1
    assert m.line_at(0) == "a"


def test_missing_lines_field_does_not_crash(qt_app):
    """A malformed snapshot missing `lines` should log+drop, not crash."""
    del qt_app
    m = MinimapModel()
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 3})  # no `lines`
    # Default of [] inside apply means empty buffer state — acceptable;
    # the alternative was crash, which we DO NOT want.
    assert m.line_count() == 0


def test_lines_not_a_list_logged_not_crashed(qt_app):
    """If `lines` is somehow a str (e.g. msgpack decode glitch), apply
    should reject + log, not iterate the string char-by-char."""
    del qt_app
    m = MinimapModel()
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 1, "lines": "not a list"})
    assert m.line_count() == 0


def test_patch_out_of_bounds_logged_not_applied(qt_app):
    """A patch range that exceeds the current buffer must be dropped,
    not silently corrupted into a partial-splice."""
    del qt_app
    m = MinimapModel()
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 3, "lines": list("abc")})
    # last > current count — out of bounds.
    m.apply(
        {
            "op": "patch",
            "bufnr": 1,
            "line_count": 3,
            "first": 0,
            "last": 99,
            "lines": ["X"],
        }
    )
    # State unchanged.
    assert [m.line_at(i) for i in range(3)] == ["a", "b", "c"]


def test_patch_negative_first_logged_not_applied(qt_app):
    del qt_app
    m = MinimapModel()
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 3, "lines": list("abc")})
    m.apply(
        {
            "op": "patch",
            "bufnr": 1,
            "line_count": 3,
            "first": -1,
            "last": 1,
            "lines": ["X"],
        }
    )
    assert m.line_at(0) == "a"


def test_apply_with_completely_invalid_payload_does_not_crash(qt_app):
    """The cross-thread defensive wrap means even total garbage doesn't
    crash. Logs + drops."""
    del qt_app
    m = MinimapModel()
    # Not even a dict — apply().get() would raise AttributeError; the
    # outer try/except swallows it.
    m.apply(None)  # type: ignore[arg-type]
    m.apply("not a dict")  # type: ignore[arg-type]
    assert m.line_count() == 0


# ---------------------------------------------------------------------------
# QML registration / context property naming
# ---------------------------------------------------------------------------


def test_app_exposes_minimap_model_context_property():
    """app.py's _build_engine must call setContextProperty("minimapModel",
    controller.minimap_model). Without this, Main.qml's
    `minimapModel.lineCount` binding is undefined-reference and the
    MinimapView's bufferRowCount stays 0."""
    import inspect

    from symmetria_ide import app

    src = inspect.getsource(app)
    assert 'setContextProperty("minimapModel"' in src, (
        "app.py's _build_engine must expose minimapModel as a context property"
    )


def test_app_imports_minimap_model():
    """AppController instantiates MinimapModel — the import must be
    present at module level."""
    import inspect

    from symmetria_ide import app

    src = inspect.getsource(app)
    assert "from .minimap_model import MinimapModel" in src


def test_app_connects_minimap_event_with_queued_connection():
    """The cross-thread connection from the pynvim worker thread to
    the GUI-thread MinimapModel.apply MUST be explicit
    Qt.QueuedConnection per §4 P2. An auto-typed connection would still
    work today (Qt auto-selects Queued when sender/receiver are on
    different threads), but project standards require explicit so the
    intent is visible at the connect site."""
    import inspect

    from symmetria_ide import app

    src = inspect.getsource(app)
    assert "minimap_event.connect" in src
    # The Qt.ConnectionType.QueuedConnection arg must appear within ~200
    # chars of the minimap_event.connect call (same scope).
    idx = src.find("minimap_event.connect")
    nearby = src[idx : idx + 400]
    assert "QueuedConnection" in nearby, (
        "minimap_event.connect must specify Qt.QueuedConnection explicitly (§4 P2)"
    )


def test_nvim_backend_subscribes_to_minimap_channel():
    """The backend must subscribe to 'minimap' — without it, notifications
    on that channel are silently dropped by pynvim. Subscribe is now
    table-driven (_CHANNEL_TO_SIGNAL); membership there is the contract."""
    from symmetria_ide.nvim_backend import NvimBackend

    assert "minimap" in NvimBackend._CHANNEL_TO_SIGNAL


def test_nvim_backend_force_pushes_minimap_snapshot_on_subscribe():
    """The subscribe-race fix (gotcha #2 mitigation): right after
    subscribing, force a Lua-side re-push so the initial BufEnter
    snapshot — which fired BEFORE we subscribed — gets re-emitted."""
    import inspect

    from symmetria_ide import nvim_backend

    src = inspect.getsource(nvim_backend)
    assert "symmetria_minimap_push_snapshot" in src, (
        "NvimBackend must force-push an initial minimap snapshot after "
        "subscribing — the Lua-side helper is _G.symmetria_minimap_push_snapshot"
    )


def test_dispatch_routes_minimap_envelope():
    """The dispatch table routes the 'minimap' channel to minimap_event."""
    from symmetria_ide.nvim_backend import NvimBackend

    assert NvimBackend._CHANNEL_TO_SIGNAL["minimap"] == "minimap_event"


# ---------------------------------------------------------------------------
# bufnr accessor — Phase 2 painters use this to invalidate per-buffer caches
# ---------------------------------------------------------------------------


def test_snapshot_updates_bufnr(qt_app):
    """bufnr() must reflect the buffer number from the most recent snapshot.
    Phase 2 painters compare this to their cached bufnr to decide whether
    to invalidate indent-level arrays and other per-buffer state."""
    del qt_app
    m = MinimapModel()
    assert m.bufnr() == -1  # initial state: no snapshot applied
    m.apply({"op": "snapshot", "bufnr": 5, "line_count": 2, "lines": ["a", "b"]})
    assert m.bufnr() == 5
    # Switching to a different buffer updates bufnr.
    m.apply({"op": "snapshot", "bufnr": 12, "line_count": 1, "lines": ["x"]})
    assert m.bufnr() == 12


def test_patch_updates_bufnr(qt_app):
    """A patch envelope must also update the tracked bufnr."""
    del qt_app
    m = MinimapModel()
    m.apply({"op": "snapshot", "bufnr": 3, "line_count": 3, "lines": list("abc")})
    assert m.bufnr() == 3
    m.apply(
        {
            "op": "patch",
            "bufnr": 7,
            "line_count": 3,
            "first": 0,
            "last": 1,
            "lines": ["A"],
        }
    )
    # bufnr comes from the patch envelope, not the prior snapshot.
    assert m.bufnr() == 7


# ---------------------------------------------------------------------------
# Phase 2 — indent-level cache (PRD §6 R2.2)
# ---------------------------------------------------------------------------


def test_indent_level_pure_function_spaces():
    """The pure helper `_compute_indent_level` is the canonical mapping
    from a line to its 0..3 rung. Two spaces == one level."""
    from symmetria_ide.minimap_model import _compute_indent_level

    assert _compute_indent_level("") == 0
    assert _compute_indent_level("top") == 0
    assert _compute_indent_level("  foo") == 1
    assert _compute_indent_level("    foo") == 2
    assert _compute_indent_level("      foo") == 3


def test_indent_level_clamps_to_max():
    """Very deeply nested code clamps to the palette's max rung rather
    than overflowing the indent palette index."""
    from symmetria_ide.minimap_model import _MAX_INDENT_LEVEL, _compute_indent_level

    assert _MAX_INDENT_LEVEL == 3
    # Far-past-max indent still returns the max rung.
    assert _compute_indent_level("                deep") == _MAX_INDENT_LEVEL
    assert _compute_indent_level("\t\t\t\t\t\t\t\t" + "deep") == _MAX_INDENT_LEVEL


def test_indent_level_tab_counts_as_one_level():
    """Tabs are one level each regardless of effective tab width — the
    minimap doesn't know about `tabstop` and at minimap scale the
    visual difference between 4-/8-space tabs is invisible."""
    from symmetria_ide.minimap_model import _compute_indent_level

    assert _compute_indent_level("\tfoo") == 1
    assert _compute_indent_level("\t\tfoo") == 2
    assert _compute_indent_level("\t\t\tfoo") == 3


def test_indent_level_mixed_tabs_and_spaces():
    """Tabs + spaces together count cumulatively. Tab=2 columns, two
    spaces=2 columns, two columns per level."""
    from symmetria_ide.minimap_model import _compute_indent_level

    # 2 spaces + 1 tab = 2 + 2 = 4 columns = level 2
    assert _compute_indent_level("  \tfoo") == 2
    # 1 tab + 2 spaces = 2 + 2 = 4 columns = level 2
    assert _compute_indent_level("\t  foo") == 2


def test_snapshot_populates_indent_cache(qt_app):
    """After a snapshot, indent_level(i) for every row must match
    `_compute_indent_level(line_at(i))`. The cache and the lines must
    stay parallel — a drift here would cause the painter to colour
    blocks with the wrong indent rung."""
    del qt_app
    from symmetria_ide.minimap_model import _compute_indent_level

    m = MinimapModel()
    lines = ["top", "  func():", "    if x:", "      pass", "    return"]
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": len(lines), "lines": lines})
    for i, line in enumerate(lines):
        assert m.indent_level(i) == _compute_indent_level(line), (
            f"indent_level({i}) drift from cached vs computed for {line!r}"
        )


def test_indent_level_out_of_range_returns_zero(qt_app):
    """Same bounds-clamping contract as line_at(). Phase 2 painter's
    iteration may briefly overshoot during stale-bookkeeping — the
    accessor must not crash."""
    del qt_app
    m = MinimapModel()
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 2, "lines": ["a", "  b"]})
    assert m.indent_level(-1) == 0
    assert m.indent_level(100) == 0


def test_patch_updates_indent_cache_only_for_affected_range(qt_app):
    """A patch must splice `_indent_levels[first:last] = new_levels`
    just like it splices `_lines`. Touching the whole cache would be
    O(N) per patch and defeat the patch's reason to exist."""
    del qt_app
    m = MinimapModel()
    m.apply(
        {
            "op": "snapshot",
            "bufnr": 1,
            "line_count": 5,
            "lines": ["A", "  B", "    C", "      D", "        E"],
        }
    )
    # Before patch: indent levels are [0, 1, 2, 3, 3] (3 = max-clamp).
    assert [m.indent_level(i) for i in range(5)] == [0, 1, 2, 3, 3]
    # Patch row 2 (was indent 2, "    C") with a level-0 line.
    m.apply(
        {
            "op": "patch",
            "bufnr": 1,
            "line_count": 5,
            "first": 2,
            "last": 3,
            "lines": ["FLAT"],
        }
    )
    # Cache must reflect: row 2 → indent 0, rest unchanged.
    assert [m.indent_level(i) for i in range(5)] == [0, 1, 0, 3, 3]


def test_patch_pure_insertion_extends_indent_cache(qt_app):
    """Inserting N new lines must grow the indent cache by N entries."""
    del qt_app
    m = MinimapModel()
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 2, "lines": ["A", "B"]})
    m.apply(
        {
            "op": "patch",
            "bufnr": 1,
            "line_count": 4,
            "first": 1,
            "last": 1,
            "lines": ["  X", "    Y"],
        }
    )
    assert m.line_count() == 4
    assert [m.indent_level(i) for i in range(4)] == [0, 1, 2, 0]


def test_patch_pure_deletion_shrinks_indent_cache(qt_app):
    """Deleting lines must shrink the indent cache to match."""
    del qt_app
    m = MinimapModel()
    m.apply(
        {
            "op": "snapshot",
            "bufnr": 1,
            "line_count": 4,
            "lines": ["A", "  B", "    C", "      D"],
        }
    )
    m.apply(
        {"op": "patch", "bufnr": 1, "line_count": 2, "first": 1, "last": 3, "lines": []}
    )
    assert m.line_count() == 2
    assert [m.indent_level(i) for i in range(2)] == [0, 3]


def test_snapshot_replaces_indent_cache(qt_app):
    """A second snapshot must wipe the prior indent cache, not append.
    Otherwise the old buffer's indents would bleed into the new one."""
    del qt_app
    m = MinimapModel()
    m.apply(
        {
            "op": "snapshot",
            "bufnr": 1,
            "line_count": 3,
            "lines": ["      deep", "      deep", "      deep"],
        }
    )
    assert [m.indent_level(i) for i in range(3)] == [3, 3, 3]
    m.apply({"op": "snapshot", "bufnr": 2, "line_count": 2, "lines": ["flat", "flat"]})
    assert [m.indent_level(i) for i in range(2)] == [0, 0]
    # Past-end must be 0, not 3 (would indicate stale cache).
    assert m.indent_level(2) == 0


# ---------------------------------------------------------------------------
# Phase 3 — viewport channel (apply_viewport ingest + signal)
# ---------------------------------------------------------------------------


def test_viewport_initial_state_is_zero(qt_app):
    """Empty model: viewport_first() and viewport_count() both return 0
    so the painter knows there's no indicator to draw yet."""
    del qt_app
    m = MinimapModel()
    assert m.viewport_first() == 0
    assert m.viewport_count() == 0


def test_apply_viewport_updates_state(qt_app):
    """A `{first, count}` envelope updates both accessors."""
    del qt_app
    m = MinimapModel()
    m.apply_viewport({"first": 10, "count": 30})
    assert m.viewport_first() == 10
    assert m.viewport_count() == 30


def test_apply_viewport_emits_signal(qt_app):
    """viewportChanged must fire when the bounds actually change."""
    del qt_app
    m = MinimapModel()
    calls: list[tuple[int, int]] = []
    m.viewportChanged.connect(
        lambda: calls.append((m.viewport_first(), m.viewport_count()))
    )
    m.apply_viewport({"first": 5, "count": 20})
    assert calls == [(5, 20)]


def test_apply_viewport_short_circuits_on_equality(qt_app):
    """Equality short-circuit: a no-op apply (same first + count) must
    NOT re-emit viewportChanged. Rapid cursor motion inside the
    viewport fires Lua-side CursorMoved repeatedly; the Python dedup
    keeps QML bindings stable."""
    del qt_app
    m = MinimapModel()
    m.apply_viewport({"first": 5, "count": 20})
    calls: list[int] = []
    m.viewportChanged.connect(lambda: calls.append(1))
    m.apply_viewport({"first": 5, "count": 20})  # same — no emit
    assert calls == []
    m.apply_viewport({"first": 6, "count": 20})  # first changed — emit
    assert calls == [1]


def test_apply_viewport_clamps_negative_values(qt_app):
    """Negative first/count are coerced to 0 — defensive against a
    future Lua-side bug emitting weird values."""
    del qt_app
    m = MinimapModel()
    m.apply_viewport({"first": -5, "count": -10})
    assert m.viewport_first() == 0
    assert m.viewport_count() == 0


def test_apply_viewport_malformed_payload_does_not_crash(qt_app):
    """The defensive try/except contract — same as apply() — must
    cover the cross-thread boundary."""
    del qt_app
    m = MinimapModel()
    # Total garbage — apply_viewport.get() would raise AttributeError.
    m.apply_viewport(None)  # type: ignore[arg-type]
    m.apply_viewport("not a dict")  # type: ignore[arg-type]
    # State unchanged — bad envelopes logged + dropped.
    assert m.viewport_first() == 0


def test_app_connects_minimap_viewport_event_with_queued_connection():
    """The cross-thread connection from pynvim worker → MinimapModel.apply_viewport
    must be explicit Qt.QueuedConnection per §4 P2. Mirrors the
    minimap_event connection's contract."""
    import inspect

    from symmetria_ide import app

    src = inspect.getsource(app)
    assert "minimap_viewport_event.connect" in src
    idx = src.find("minimap_viewport_event.connect")
    nearby = src[idx : idx + 400]
    assert "QueuedConnection" in nearby, (
        "minimap_viewport_event.connect must specify Qt.QueuedConnection "
        "explicitly (§4 P2)"
    )


def test_nvim_backend_subscribes_to_minimap_viewport_channel():
    """The backend must subscribe to 'minimap_viewport' (table-driven)."""
    from symmetria_ide.nvim_backend import NvimBackend

    assert "minimap_viewport" in NvimBackend._CHANNEL_TO_SIGNAL


def test_nvim_backend_force_pushes_minimap_viewport():
    """Subscribe-race fix for the viewport channel — Lua-side helper
    is _G.symmetria_minimap_push_viewport."""
    import inspect

    from symmetria_ide import nvim_backend

    src = inspect.getsource(nvim_backend)
    assert "symmetria_minimap_push_viewport" in src


def test_dispatch_routes_minimap_viewport_envelope():
    """The dispatch table routes 'minimap_viewport' to its signal."""
    from symmetria_ide.nvim_backend import NvimBackend

    assert (
        NvimBackend._CHANNEL_TO_SIGNAL["minimap_viewport"] == "minimap_viewport_event"
    )


def test_seek_to_row_uses_async_call_and_1_indexed_goto():
    """AppController.seek_to_row must (a) marshal through nvim.async_call
    (gotcha #1 — pynvim isn't thread-safe and QML calls this on GUI
    thread) and (b) issue a 1-indexed `normal! NG` jump (nvim's :goto
    counts from 1; the @Slot signature counts from 0)."""
    import inspect

    from symmetria_ide.app import AppController

    src = inspect.getsource(AppController.seek_to_row)
    assert "async_call" in src, (
        "seek_to_row must marshal to the loop thread via nvim.async_call "
        "(gotcha #1) — direct nvim.command from the GUI thread raises"
    )
    assert "row + 1" in src, (
        "seek_to_row must convert 0-indexed row to 1-indexed nvim line "
        "before the :normal! NG jump"
    )
    assert "normal!" in src, (
        "seek_to_row must use `normal!` (bang) to suppress remaps of G"
    )


# ---------------------------------------------------------------------------
# Phase 4 — diagnostic + git-diff ingest
# ---------------------------------------------------------------------------


def test_diagnostic_initial_state_is_empty(qt_app):
    """Empty model: diagnostic_at always returns "" and diagnostic_count is 0."""
    del qt_app
    m = MinimapModel()
    assert m.diagnostic_at(0) == ""
    assert m.diagnostic_at(100) == ""
    assert m.diagnostic_count() == 0


def test_git_initial_state_is_empty(qt_app):
    """Empty model: git_at always returns "" and git_count is 0."""
    del qt_app
    m = MinimapModel()
    assert m.git_at(0) == ""
    assert m.git_at(100) == ""
    assert m.git_count() == 0


def test_apply_diagnostics_populates_dict(qt_app):
    """A list of entries becomes the lnum→severity map."""
    del qt_app
    m = MinimapModel()
    m.apply_diagnostics(
        {
            "bufnr": 1,
            "entries": [
                {"lnum": 3, "severity": "error"},
                {"lnum": 7, "severity": "warn"},
                {"lnum": 12, "severity": "info"},
                {"lnum": 20, "severity": "hint"},
            ],
        }
    )
    assert m.diagnostic_at(3) == "error"
    assert m.diagnostic_at(7) == "warn"
    assert m.diagnostic_at(12) == "info"
    assert m.diagnostic_at(20) == "hint"
    assert m.diagnostic_count() == 4


def test_apply_diagnostics_max_severity_wins_on_collision(qt_app):
    """Multiple entries on the same line collapse to the highest-rank one
    per PRD §8.3 R4.2 — minimap dot scale can't show multiple severities."""
    del qt_app
    m = MinimapModel()
    m.apply_diagnostics(
        {
            "bufnr": 1,
            "entries": [
                {"lnum": 5, "severity": "warn"},
                {"lnum": 5, "severity": "error"},  # error > warn → wins
                {"lnum": 5, "severity": "hint"},
            ],
        }
    )
    assert m.diagnostic_at(5) == "error"


def test_apply_diagnostics_short_circuits_on_equal_state(qt_app):
    """Equivalent payloads (different entry order) must NOT re-emit
    diagnosticsChanged. LSP servers republish identical sets often."""
    del qt_app
    m = MinimapModel()
    m.apply_diagnostics(
        {
            "bufnr": 1,
            "entries": [
                {"lnum": 3, "severity": "error"},
                {"lnum": 7, "severity": "warn"},
            ],
        }
    )
    calls: list[int] = []
    m.diagnosticsChanged.connect(lambda: calls.append(1))
    # Same final dict, different entry order — should be a no-op.
    m.apply_diagnostics(
        {
            "bufnr": 1,
            "entries": [
                {"lnum": 7, "severity": "warn"},
                {"lnum": 3, "severity": "error"},
            ],
        }
    )
    assert calls == []
    # Adding a new entry triggers the emit.
    m.apply_diagnostics(
        {
            "bufnr": 1,
            "entries": [
                {"lnum": 3, "severity": "error"},
                {"lnum": 7, "severity": "warn"},
                {"lnum": 11, "severity": "hint"},
            ],
        }
    )
    assert calls == [1]


def test_apply_diagnostics_drops_invalid_entries(qt_app):
    """Malformed entries (non-dict, missing fields, wrong types,
    negative lnums) are silently dropped — defensive."""
    del qt_app
    m = MinimapModel()
    m.apply_diagnostics(
        {
            "bufnr": 1,
            "entries": [
                {"lnum": 3, "severity": "error"},  # OK
                "not a dict",  # dropped
                {"lnum": 7},  # missing severity — dropped
                {"severity": "warn"},  # missing lnum — dropped
                {"lnum": "five", "severity": "hint"},  # wrong type — dropped
                {"lnum": -2, "severity": "warn"},  # negative — dropped
            ],
        }
    )
    assert m.diagnostic_at(3) == "error"
    assert m.diagnostic_count() == 1


def test_apply_diagnostics_malformed_payload_does_not_crash(qt_app):
    """Same cross-thread defensive contract as apply() / apply_viewport."""
    del qt_app
    m = MinimapModel()
    m.apply_diagnostics(None)  # type: ignore[arg-type]
    m.apply_diagnostics("not a dict")  # type: ignore[arg-type]
    assert m.diagnostic_count() == 0


def test_apply_git_populates_dict(qt_app):
    """A list of entries becomes the lnum→kind map."""
    del qt_app
    m = MinimapModel()
    m.apply_git(
        {
            "bufnr": 1,
            "entries": [
                {"lnum": 3, "kind": "added"},
                {"lnum": 7, "kind": "modified"},
                {"lnum": 11, "kind": "deleted"},
            ],
        }
    )
    assert m.git_at(3) == "added"
    assert m.git_at(7) == "modified"
    assert m.git_at(11) == "deleted"
    assert m.git_count() == 3


def test_apply_git_drops_invalid_kinds(qt_app):
    """Unknown `kind` strings are dropped — protects against future
    gitsigns versions emitting kinds we don't have a colour for."""
    del qt_app
    m = MinimapModel()
    m.apply_git(
        {
            "bufnr": 1,
            "entries": [
                {"lnum": 3, "kind": "added"},
                {"lnum": 5, "kind": "renamed"},  # not in _GIT_KINDS → dropped
            ],
        }
    )
    assert m.git_count() == 1
    assert m.git_at(3) == "added"
    assert m.git_at(5) == ""


def test_apply_git_short_circuits_on_equal_state(qt_app):
    """Equivalent payloads don't re-emit gitChanged."""
    del qt_app
    m = MinimapModel()
    m.apply_git(
        {
            "bufnr": 1,
            "entries": [
                {"lnum": 3, "kind": "added"},
            ],
        }
    )
    calls: list[int] = []
    m.gitChanged.connect(lambda: calls.append(1))
    m.apply_git(
        {
            "bufnr": 1,
            "entries": [
                {"lnum": 3, "kind": "added"},
            ],
        }
    )
    assert calls == []


def test_apply_git_malformed_payload_does_not_crash(qt_app):
    del qt_app
    m = MinimapModel()
    m.apply_git(None)  # type: ignore[arg-type]
    m.apply_git("garbage")  # type: ignore[arg-type]
    assert m.git_count() == 0


def test_app_connects_phase4_signals_with_queued_connection():
    """Both minimap_diagnostics_event and minimap_git_event must use
    explicit Qt.QueuedConnection per §4 P2 — they originate on the
    pynvim worker thread."""
    import inspect

    from symmetria_ide import app

    src = inspect.getsource(app)
    for sig in ("minimap_diagnostics_event.connect", "minimap_git_event.connect"):
        assert sig in src, f"{sig} missing from app.py wiring"
        idx = src.find(sig)
        nearby = src[idx : idx + 400]
        assert "QueuedConnection" in nearby, (
            f"{sig} must specify Qt.QueuedConnection explicitly (§4 P2)"
        )


def test_nvim_backend_subscribes_to_phase4_channels():
    """The backend must subscribe to both Phase 4 channels (table-driven)."""
    from symmetria_ide.nvim_backend import NvimBackend

    assert "minimap_diagnostics" in NvimBackend._CHANNEL_TO_SIGNAL
    assert "minimap_git" in NvimBackend._CHANNEL_TO_SIGNAL


def test_nvim_backend_force_pushes_phase4_helpers():
    """Subscribe-race fixes — the Lua helpers must be invoked
    immediately after subscribing."""
    import inspect

    from symmetria_ide import nvim_backend

    src = inspect.getsource(nvim_backend)
    assert "symmetria_minimap_push_diagnostics" in src
    assert "symmetria_minimap_push_git" in src


def test_dispatch_routes_phase4_envelopes():
    """The dispatch table routes both Phase 4 channels to their signals."""
    from symmetria_ide.nvim_backend import NvimBackend

    assert (
        NvimBackend._CHANNEL_TO_SIGNAL["minimap_diagnostics"]
        == "minimap_diagnostics_event"
    )
    assert NvimBackend._CHANNEL_TO_SIGNAL["minimap_git"] == "minimap_git_event"


# ---------------------------------------------------------------------------
# Phase 4.5 — content-length cache (line-width fidelity)
# ---------------------------------------------------------------------------


def test_compute_line_metrics_returns_indent_and_length(qt_app):
    """The unified pure-fn must return (indent_level, content_length)
    in a single walk — saves a second per-line walk on apply paths."""
    del qt_app
    from symmetria_ide.minimap_model import _compute_line_metrics

    assert _compute_line_metrics("") == (0, 0)
    assert _compute_line_metrics("foo") == (0, 3)
    assert _compute_line_metrics("  foo") == (1, 3)
    assert _compute_line_metrics("    foo") == (2, 3)
    assert _compute_line_metrics("      foo") == (3, 3)


def test_content_length_excludes_leading_whitespace(qt_app):
    """Content length must count chars AFTER leading whitespace —
    the indent offset already represents that whitespace visually."""
    del qt_app
    from symmetria_ide.minimap_model import _compute_line_metrics

    _, content = _compute_line_metrics("    hello world")
    assert content == len("hello world")


def test_content_length_zero_for_blank_and_pure_whitespace(qt_app):
    """Empty / pure-space / pure-tab lines all return content_length=0
    so the painter skips them as gaps in the silhouette."""
    del qt_app
    from symmetria_ide.minimap_model import _compute_line_metrics

    assert _compute_line_metrics("")[1] == 0
    assert _compute_line_metrics("    ")[1] == 0
    assert _compute_line_metrics("\t\t")[1] == 0


def test_content_length_counts_utf8_codepoints_not_bytes(qt_app):
    """`len(str)` returns codepoint count which is the right unit at
    minimap scale — multi-byte UTF-8 chars count as 1, not 2 or 3."""
    del qt_app
    from symmetria_ide.minimap_model import _compute_line_metrics

    # Emoji is 1 codepoint but multiple UTF-8 bytes.
    assert _compute_line_metrics("hi 😀")[1] == 4
    # Accented chars: 1 codepoint each.
    assert _compute_line_metrics("café")[1] == 4


def test_snapshot_populates_content_length_cache(qt_app):
    """apply_snapshot must build _content_lengths parallel to
    _indent_levels and _lines."""
    del qt_app
    m = MinimapModel()
    m.apply(
        {
            "op": "snapshot",
            "bufnr": 1,
            "line_count": 4,
            "lines": ["abc", "  def", "    g", ""],
        }
    )
    assert m.content_length(0) == 3
    assert m.content_length(1) == 3
    assert m.content_length(2) == 1
    assert m.content_length(3) == 0


def test_content_length_oob_clamps_to_zero(qt_app):
    """Same bounds-clamping contract as indent_level()."""
    del qt_app
    m = MinimapModel()
    m.apply({"op": "snapshot", "bufnr": 1, "line_count": 2, "lines": ["a", "b"]})
    assert m.content_length(-1) == 0
    assert m.content_length(100) == 0


def test_patch_updates_content_length_cache(qt_app):
    """A patch must splice _content_lengths over the affected range —
    same range the _indent_levels splice touches."""
    del qt_app
    m = MinimapModel()
    m.apply(
        {"op": "snapshot", "bufnr": 1, "line_count": 3, "lines": ["abc", "def", "ghi"]}
    )
    assert [m.content_length(i) for i in range(3)] == [3, 3, 3]
    m.apply(
        {
            "op": "patch",
            "bufnr": 1,
            "line_count": 3,
            "first": 1,
            "last": 2,
            "lines": ["XXXXXXXX"],
        }
    )
    assert m.content_length(1) == 8
    # Other rows untouched.
    assert m.content_length(0) == 3
    assert m.content_length(2) == 3


def test_snapshot_replaces_full_content_length_cache(qt_app):
    """A fresh snapshot must wipe the prior content_length cache —
    no bleed-through from a longer prior buffer."""
    del qt_app
    m = MinimapModel()
    m.apply(
        {
            "op": "snapshot",
            "bufnr": 1,
            "line_count": 3,
            "lines": ["x" * 50, "y" * 50, "z" * 50],
        }
    )
    m.apply({"op": "snapshot", "bufnr": 2, "line_count": 1, "lines": ["short"]})
    assert m.content_length(0) == 5
    # Past-end clamp — prior 50s must not bleed through.
    assert m.content_length(1) == 0
    assert m.content_length(2) == 0


def test_patch_pure_insertion_extends_content_length_cache(qt_app):
    """Inserting N new lines must grow _content_lengths by N entries,
    keeping it parallel with _indent_levels and _lines."""
    del qt_app
    m = MinimapModel()
    m.apply(
        {"op": "snapshot", "bufnr": 1, "line_count": 3, "lines": ["ab", "cde", "f"]}
    )
    assert [m.content_length(i) for i in range(3)] == [2, 3, 1]
    # Pure insertion at line 1 (first == last == 1)
    m.apply(
        {
            "op": "patch",
            "bufnr": 1,
            "line_count": 5,
            "first": 1,
            "last": 1,
            "lines": ["XXXX", "YYYYY"],
        }
    )
    assert m.line_count() == 5
    assert [m.content_length(i) for i in range(5)] == [2, 4, 5, 3, 1]


def test_patch_pure_deletion_shrinks_content_length_cache(qt_app):
    """Deleting lines must shrink _content_lengths to match, keeping
    it parallel with _indent_levels and _lines."""
    del qt_app
    m = MinimapModel()
    m.apply(
        {
            "op": "snapshot",
            "bufnr": 1,
            "line_count": 4,
            "lines": ["a", "bb", "ccc", "dddd"],
        }
    )
    assert [m.content_length(i) for i in range(4)] == [1, 2, 3, 4]
    # Pure deletion of lines 1 and 2
    m.apply(
        {
            "op": "patch",
            "bufnr": 1,
            "line_count": 2,
            "first": 1,
            "last": 3,
            "lines": [],
        }
    )
    assert m.line_count() == 2
    assert [m.content_length(i) for i in range(2)] == [1, 4]
