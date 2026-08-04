"""Tests for `GhPrController` — the git surface's GitHub PR lane.

Three layers, mirroring the sibling git tests:

  - Pure module-level helpers (`relative_time`, `classify_gh_failure`,
    `parse_concatenated_json_arrays`, `parse_pr_list`, `flatten_timeline`) —
    no Qt, no subprocess, no network. Canned JSON fixtures as constants.
  - Worker apply methods (`_do_list` / `_do_detail`) called DIRECTLY on the
    test thread with `_run_gh` + `resolve_repo_root` monkeypatched — same
    call-the-private-method style as `test_git_ops_controller.py`'s
    `_do_pull` tests. No event loop / NO `processEvents`, per the shared-app
    SEGV memo in `.claude/memory`.
  - Dispatch/worker contract: the checkout single-flight `_busy` gate, the
    no-repo failure emit, `_busy` recovery after a worker exception, and the
    toggle→refetch argv. Worker-side signals are captured via an explicit
    `DirectConnection` + `threading.Event`.

See `src/symmetria_ide/gh_pr_controller.py`.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime

from PySide6.QtCore import Qt

import symmetria_ide.gh_pr_controller as gh_mod
from symmetria_ide.gh_pr_controller import (
    GhPrController,
    classify_gh_failure,
    flatten_timeline,
    parse_concatenated_json_arrays,
    parse_pr_list,
    relative_time,
)

_NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# relative_time
# ---------------------------------------------------------------------------


def test_relative_time_boundaries() -> None:
    assert relative_time("2026-07-12T11:59:30Z", _NOW) == "just now"
    assert relative_time("2026-07-12T11:55:00Z", _NOW) == "5m ago"
    assert relative_time("2026-07-12T09:00:00Z", _NOW) == "3h ago"
    assert relative_time("2026-07-09T12:00:00Z", _NOW) == "3d ago"
    assert relative_time("2026-06-28T12:00:00Z", _NOW) == "2w ago"
    assert relative_time("2026-05-01T12:00:00Z", _NOW) == "2mo ago"
    assert relative_time("2024-07-12T12:00:00Z", _NOW) == "2y ago"


def test_relative_time_degrades_on_bad_input() -> None:
    assert relative_time("", _NOW) == ""
    assert relative_time("not-a-date", _NOW) == ""


# ---------------------------------------------------------------------------
# parse_concatenated_json_arrays — the `gh api --paginate` quirk
# ---------------------------------------------------------------------------


def test_paginate_parser_single_array() -> None:
    assert parse_concatenated_json_arrays('[{"a": 1}, {"b": 2}]') == [
        {"a": 1},
        {"b": 2},
    ]


def test_paginate_parser_concatenated_arrays() -> None:
    # Two pages back-to-back — the shape plain json.loads chokes on.
    assert parse_concatenated_json_arrays('[{"a": 1}][{"b": 2}]') == [
        {"a": 1},
        {"b": 2},
    ]


def test_paginate_parser_empty_and_whitespace() -> None:
    assert parse_concatenated_json_arrays("") == []
    assert parse_concatenated_json_arrays("   \n  ") == []
    assert parse_concatenated_json_arrays('[{"a": 1}]\n') == [{"a": 1}]


def test_paginate_parser_skips_non_array_docs_and_garbage() -> None:
    # A non-array document is skipped; trailing garbage stops the loop
    # without raising.
    assert parse_concatenated_json_arrays('{"msg": "nope"}[{"a": 1}]xx') == [{"a": 1}]


# ---------------------------------------------------------------------------
# parse_pr_list
# ---------------------------------------------------------------------------

_LIST_JSON = json.dumps(
    [
        {
            "number": 7,
            "title": "Add PR viewer",
            "author": {"login": "jc"},
            "createdAt": "2026-07-10T10:00:00Z",
            "updatedAt": "2026-07-12T09:00:00Z",
            "state": "OPEN",
            "headRefName": "feat/pr-viewer",
            "isDraft": True,
            "reviewDecision": "REVIEW_REQUIRED",
            "url": "https://github.com/o/r/pull/7",
        },
        {"number": "not-an-int"},  # malformed — skipped, not fatal
        {
            "number": 5,
            "title": "Fix thing",
            "author": None,  # deleted account
            "createdAt": "2026-07-01T10:00:00Z",
            "updatedAt": "2026-07-01T10:00:00Z",
            "state": "MERGED",
            "headRefName": "fix/thing",
            "isDraft": False,
            "reviewDecision": None,
            "url": "https://github.com/o/r/pull/5",
        },
    ]
)


def test_parse_pr_list_maps_fields_and_skips_malformed() -> None:
    rows = parse_pr_list(_LIST_JSON, _NOW)
    assert [r.number for r in rows] == [7, 5]
    first = rows[0]
    assert first.title == "Add PR viewer"
    assert first.author_login == "jc"
    assert first.is_draft is True
    assert first.review_decision == "REVIEW_REQUIRED"
    assert first.relative_updated == "3h ago"
    # Null author → ghost; null reviewDecision → "".
    assert rows[1].author_login == "ghost"
    assert rows[1].review_decision == ""
    assert rows[1].state == "MERGED"


def test_parse_pr_list_degrades_on_bad_json() -> None:
    assert parse_pr_list("", _NOW) == []
    assert parse_pr_list("not json", _NOW) == []
    assert parse_pr_list('{"an": "object"}', _NOW) == []


# ---------------------------------------------------------------------------
# flatten_timeline
# ---------------------------------------------------------------------------

_DETAIL = {
    "comments": [
        {
            "author": {"login": "alice"},
            "body": "First comment",
            "createdAt": "2026-07-10T10:00:00Z",
        },
        {
            "author": None,
            "body": "Ghost comment",
            "createdAt": "2026-07-10T12:00:00Z",
        },
    ],
    "reviews": [
        {
            "author": {"login": "bob"},
            "body": "",
            "state": "COMMENTED",  # empty container for inline batch — dropped
            "submittedAt": "2026-07-10T11:00:00Z",
        },
        {
            "author": {"login": "bob"},
            "body": "LGTM",
            "state": "APPROVED",
            "submittedAt": "2026-07-11T09:00:00Z",
        },
    ],
}

_INLINE = [
    {
        "user": {"login": "bob"},
        "body": "Rename this",
        "created_at": "2026-07-10T11:00:01Z",
        "path": "src/foo.py",
        "line": 42,
        "original_line": 40,
    },
    {
        "user": {"login": "bob"},
        "body": "Outdated hunk note",
        "created_at": "2026-07-10T11:00:02Z",
        "path": "src/bar.py",
        "line": None,  # outdated diff → fall back to original_line
        "original_line": 7,
    },
]


def test_flatten_timeline_merges_and_orders_chronologically() -> None:
    entries = flatten_timeline(_DETAIL, _INLINE, _NOW)
    assert [(e.kind, e.author) for e in entries] == [
        ("comment", "alice"),
        ("inline", "bob"),
        ("inline", "bob"),
        ("comment", "ghost"),
        ("review", "bob"),
    ]
    # The empty-body COMMENTED review was dropped; the APPROVED one kept.
    reviews = [e for e in entries if e.kind == "review"]
    assert len(reviews) == 1
    assert reviews[0].review_state == "APPROVED"
    assert reviews[0].body == "LGTM"


def test_flatten_timeline_inline_locations() -> None:
    entries = flatten_timeline(_DETAIL, _INLINE, _NOW)
    locations = [e.location for e in entries if e.kind == "inline"]
    assert locations == ["src/foo.py:42", "src/bar.py:7"]
    # Non-inline entries carry no location.
    assert all(e.location == "" for e in entries if e.kind != "inline")


def test_flatten_timeline_empty_detail() -> None:
    assert flatten_timeline({}, [], _NOW) == []


# ---------------------------------------------------------------------------
# classify_gh_failure
# ---------------------------------------------------------------------------


def test_classify_gh_failure_branches() -> None:
    assert "not installed" in classify_gh_failure(
        -1, "Could not run gh: [Errno 2] No such file"
    )
    assert "timed out" in classify_gh_failure(-1, "gh pr timed out after 30s.")
    assert "gh auth login" in classify_gh_failure(
        1, "To get started with GitHub CLI, please run:  gh auth login"
    )
    assert "No GitHub remote" in classify_gh_failure(1, "no git remotes found")
    assert "No GitHub remote" in classify_gh_failure(
        1, "could not determine base repo: ..."
    )
    # Generic failure → the stderr tail, not a canned sentence.
    assert classify_gh_failure(1, "something odd\nhappened here") == (
        "something odd\nhappened here"
    )
    assert classify_gh_failure(1, "") == "gh command failed."


# ---------------------------------------------------------------------------
# Controller — worker apply methods called directly (no event loop)
# ---------------------------------------------------------------------------


def _fake_run_gh(responses: dict[str, tuple[int, str, str]], calls: list):
    """A `_run_gh` stand-in keyed on the gh subcommand path.

    `responses` maps a key ("list" / "view" / "api" / "checkout") to the
    canned `(rc, stdout, stderr)`. Every call's argv is appended to `calls`
    for argv assertions.
    """

    def run(cwd: str, args: list[str], *, timeout: float) -> tuple[int, str, str]:
        calls.append(list(args))
        if args[0] == "api":
            key = "api"
        elif args[0] == "pr":
            key = args[1]  # list / view / checkout
        else:
            key = args[0]
        return responses.get(key, (1, "", "unexpected gh call in test"))

    return run


def test_do_list_populates_rows_and_clears_loading(monkeypatch) -> None:
    controller = GhPrController()
    try:
        monkeypatch.setattr(gh_mod, "resolve_repo_root", lambda asked: asked)
        calls: list = []
        monkeypatch.setattr(
            controller,
            "_run_gh",
            _fake_run_gh({"list": (0, _LIST_JSON, "")}, calls),
        )
        controller.set_repo_root("/repo")
        with controller._lock:
            controller._list_loading = True  # what refresh() would set
        controller._do_list(asked_root="/repo", state_filter="open")
        rows = controller.pr_rows()
        assert [r.number for r in rows] == [7, 5]
        assert controller.listLoading is False
        assert controller.listError == ""
        with controller._lock:
            assert controller._loaded_key == ("/repo", "open")
            assert controller._resolved_root == "/repo"
        assert calls[0][:4] == ["pr", "list", "--state", "open"]
    finally:
        controller.stop()


def test_do_list_failure_sets_classified_error(monkeypatch) -> None:
    controller = GhPrController()
    try:
        monkeypatch.setattr(gh_mod, "resolve_repo_root", lambda asked: asked)
        monkeypatch.setattr(
            controller,
            "_run_gh",
            _fake_run_gh(
                {"list": (-1, "", "Could not run gh: [Errno 2] No such file")},
                [],
            ),
        )
        controller.set_repo_root("/repo")
        controller._do_list(asked_root="/repo", state_filter="open")
        assert controller.pr_rows() == []
        assert "not installed" in controller.listError
        with controller._lock:
            # A failed fetch must NOT latch ensure_loaded — the next tab
            # entry should retry.
            assert controller._loaded_key is None
    finally:
        controller.stop()


def test_do_list_race_guard_drops_stale_result(monkeypatch) -> None:
    # A result fetched for repo A must not land after a switch to repo B —
    # and a result for the old state filter must not land after a toggle.
    controller = GhPrController()
    try:
        monkeypatch.setattr(gh_mod, "resolve_repo_root", lambda asked: asked)
        monkeypatch.setattr(
            controller,
            "_run_gh",
            _fake_run_gh({"list": (0, _LIST_JSON, "")}, []),
        )
        controller.set_repo_root("/repo-b")
        controller._do_list(asked_root="/repo-a", state_filter="open")
        assert controller.pr_rows() == []  # stale root dropped
        controller._do_list(asked_root="/repo-b", state_filter="closed")
        assert controller.pr_rows() == []  # stale filter dropped
        controller._do_list(asked_root="/repo-b", state_filter="open")
        assert len(controller.pr_rows()) == 2  # matching request lands
    finally:
        controller.stop()


def test_set_repo_root_resets_everything(monkeypatch) -> None:
    controller = GhPrController()
    try:
        monkeypatch.setattr(gh_mod, "resolve_repo_root", lambda asked: asked)
        monkeypatch.setattr(
            controller,
            "_run_gh",
            _fake_run_gh({"list": (0, _LIST_JSON, "")}, []),
        )
        controller.set_repo_root("/repo")
        with controller._lock:
            controller._state_filter = "closed"
        controller._do_list(asked_root="/repo", state_filter="closed")
        assert len(controller.pr_rows()) == 2
        controller.set_repo_root("/other")
        assert controller.pr_rows() == []
        assert controller.stateFilter == "open"
        assert controller.detailNumber == 0
        with controller._lock:
            assert controller._loaded_key is None
            assert controller._resolved_root == ""
    finally:
        controller.stop()


def test_ensure_loaded_fetches_once_per_key(monkeypatch) -> None:
    controller = GhPrController()
    refresh_calls: list = []
    try:
        monkeypatch.setattr(controller, "refresh", lambda: refresh_calls.append(True))
        controller.set_repo_root("/repo")
        controller.ensure_loaded()
        assert len(refresh_calls) == 1
        # Simulate the fetch having landed for this key → next entry no-ops.
        with controller._lock:
            controller._loaded_key = ("/repo", "open")
        controller.ensure_loaded()
        assert len(refresh_calls) == 1
        # A filter change invalidates the latch.
        with controller._lock:
            controller._state_filter = "closed"
        controller.ensure_loaded()
        assert len(refresh_calls) == 2
    finally:
        controller.stop()


def test_toggle_state_filter_flips_and_refetches_with_closed(monkeypatch) -> None:
    controller = GhPrController()
    fetched = threading.Event()
    calls: list = []

    def run(cwd: str, args: list[str], *, timeout: float) -> tuple[int, str, str]:
        calls.append(list(args))
        fetched.set()
        return (0, "[]", "")

    try:
        monkeypatch.setattr(gh_mod, "resolve_repo_root", lambda asked: asked)
        monkeypatch.setattr(controller, "_run_gh", run)
        controller.set_repo_root("/repo")
        controller.toggle_state_filter()
        assert controller.stateFilter == "closed"
        assert fetched.wait(timeout=5.0), "toggle never reached the worker"
        assert calls[0][:4] == ["pr", "list", "--state", "closed"]
    finally:
        controller.stop()


# ---------------------------------------------------------------------------
# Controller — detail
# ---------------------------------------------------------------------------

_DETAIL_JSON = json.dumps(
    {
        "number": 7,
        "title": "Add PR viewer",
        "body": "The description.",
        "state": "OPEN",
        "author": {"login": "jc"},
        "headRefName": "feat/pr-viewer",
        "baseRefName": "main",
        "isDraft": False,
        "createdAt": "2026-07-10T10:00:00Z",
        "mergedAt": None,
        "reviewDecision": "APPROVED",
        "url": "https://github.com/o/r/pull/7",
        "additions": 100,
        "deletions": 20,
        "changedFiles": 4,
        "comments": _DETAIL["comments"],
        "reviews": _DETAIL["reviews"],
    }
)


def test_do_detail_publishes_header_and_timeline(monkeypatch) -> None:
    controller = GhPrController()
    try:
        monkeypatch.setattr(
            controller,
            "_run_gh",
            _fake_run_gh(
                {
                    "view": (0, _DETAIL_JSON, ""),
                    "api": (0, json.dumps(_INLINE), ""),
                },
                [],
            ),
        )
        with controller._lock:
            controller._repo_root = "/repo"
            controller._resolved_root = "/repo"
        controller._do_detail(resolved_root="/repo", number=7)
        assert controller.detailNumber == 7
        assert controller.detailTitle == "Add PR viewer"
        assert controller.detailBody == "The description."
        assert controller.detailAuthor == "jc"
        assert controller.detailHeadRef == "feat/pr-viewer"
        assert controller.detailBaseRef == "main"
        assert controller.detailAdditions == 100
        assert controller.detailError == ""
        kinds = [e.kind for e in controller.timeline_entries()]
        assert kinds.count("comment") == 2
        assert kinds.count("review") == 1  # empty COMMENTED dropped
        assert kinds.count("inline") == 2
    finally:
        controller.stop()


def test_do_detail_degrades_without_inline_comments(monkeypatch) -> None:
    # REST failure must not blank the pane — conversation still publishes.
    controller = GhPrController()
    try:
        monkeypatch.setattr(
            controller,
            "_run_gh",
            _fake_run_gh(
                {
                    "view": (0, _DETAIL_JSON, ""),
                    "api": (1, "", "boom"),
                },
                [],
            ),
        )
        with controller._lock:
            controller._resolved_root = "/repo"
        controller._do_detail(resolved_root="/repo", number=7)
        assert controller.detailNumber == 7
        assert controller.detailError == ""
        kinds = [e.kind for e in controller.timeline_entries()]
        assert "inline" not in kinds
        assert kinds.count("comment") == 2
    finally:
        controller.stop()


def test_do_detail_failure_sets_error_for_that_number(monkeypatch) -> None:
    controller = GhPrController()
    try:
        monkeypatch.setattr(
            controller,
            "_run_gh",
            _fake_run_gh({"view": (1, "", "no pull requests found")}, []),
        )
        with controller._lock:
            controller._resolved_root = "/repo"
        controller._do_detail(resolved_root="/repo", number=99)
        assert controller.detailNumber == 99
        assert controller.detailError != ""
        assert controller.timeline_entries() == []
    finally:
        controller.stop()


def test_do_detail_race_guard_drops_stale_repo(monkeypatch) -> None:
    controller = GhPrController()
    try:
        monkeypatch.setattr(
            controller,
            "_run_gh",
            _fake_run_gh({"view": (0, _DETAIL_JSON, ""), "api": (0, "[]", "")}, []),
        )
        with controller._lock:
            controller._resolved_root = "/other-repo"
        controller._do_detail(resolved_root="/repo", number=7)
        assert controller.detailNumber == 0  # dropped
    finally:
        controller.stop()


# ---------------------------------------------------------------------------
# Controller — checkout dispatch/worker contract
# ---------------------------------------------------------------------------


def test_checkout_with_no_repo_root_emits_failure() -> None:
    controller = GhPrController()
    captured: list = []
    controller.operationFinished.connect(
        lambda op, ok, msg: captured.append((op, ok, msg)),
        Qt.ConnectionType.DirectConnection,
    )
    try:
        controller.checkout(7)
        assert len(captured) == 1
        op, ok, _msg = captured[0]
        assert op == "checkout"
        assert ok is False
        with controller._lock:
            assert controller._busy is False
    finally:
        controller.stop()


def test_checkout_single_flight_drops_second_request() -> None:
    controller = GhPrController()
    started: list = []
    controller.operationStarted.connect(
        lambda op: started.append(op), Qt.ConnectionType.DirectConnection
    )
    try:
        controller.set_repo_root("/some/repo")
        with controller._lock:
            controller._busy = True  # simulate an op already running
        controller.checkout(7)
        assert started == []
        assert controller._queue.empty()
    finally:
        with controller._lock:
            controller._busy = False
        controller.stop()


def test_checkout_success_emits_and_clears_busy(monkeypatch) -> None:
    controller = GhPrController()
    done = threading.Event()
    captured: list = []

    def _capture(op, ok, msg) -> None:
        captured.append((op, ok, msg))
        done.set()

    # DirectConnection → runs in the worker thread that emits; the Event is
    # set without a main-thread event loop.
    controller.operationFinished.connect(_capture, Qt.ConnectionType.DirectConnection)
    try:
        monkeypatch.setattr(
            controller,
            "_run_gh",
            _fake_run_gh(
                {"checkout": (0, "", "Switched to branch 'feat/pr-viewer'")},
                [],
            ),
        )
        controller.set_repo_root("/repo")
        controller.checkout(7)
        assert done.wait(timeout=5.0), "worker never emitted operationFinished"
        op, ok, msg = captured[0]
        assert (op, ok) == ("checkout", True)
        assert "feat/pr-viewer" in msg
        with controller._lock:
            assert controller._busy is False
    finally:
        controller.stop()


def test_busy_cleared_after_worker_exception(monkeypatch) -> None:
    # If _do_checkout raises inside the worker, _busy MUST clear and an
    # ok=False operationFinished must still fire — otherwise the single-flight
    # gate drops every future checkout forever.
    controller = GhPrController()
    done = threading.Event()
    captured: list = []

    def _capture(op, ok, msg) -> None:
        captured.append((op, ok, msg))
        done.set()

    controller.operationFinished.connect(_capture, Qt.ConnectionType.DirectConnection)

    def _boom(**_kwargs) -> None:
        raise RuntimeError("boom")

    try:
        monkeypatch.setattr(controller, "_do_checkout", _boom)
        controller.set_repo_root("/repo")
        controller.checkout(7)
        assert done.wait(timeout=5.0), "worker never emitted operationFinished"
        op, ok, _msg = captured[0]
        assert (op, ok) == ("checkout", False)
        with controller._lock:
            assert controller._busy is False
    finally:
        controller.stop()


def test_list_loading_cleared_after_worker_exception(monkeypatch) -> None:
    # The list twin of the checkout recovery test: refresh() latches
    # _list_loading on the GUI thread and only _do_list's apply clears it.
    # If _do_list raises inside the worker, the except branch MUST reset the
    # flag (and surface an error) — otherwise the pane shows
    # "loading pull requests…" forever.
    controller = GhPrController()

    def _boom(**_kwargs) -> None:
        raise RuntimeError("boom")

    try:
        monkeypatch.setattr(controller, "_do_list", _boom)
        controller.set_repo_root("/repo")
        controller.refresh()
        # Poll the flag rather than waiting on listMetaChanged: refresh()
        # emits it synchronously BEFORE the worker's recovery emit, and an
        # Event-based wait would race the two (clear/set interleaving).
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with controller._lock:
                if not controller._list_loading:
                    break
            time.sleep(0.01)
        assert controller.listLoading is False, "loading flag never recovered"
        assert controller.listError != ""
    finally:
        controller.stop()
