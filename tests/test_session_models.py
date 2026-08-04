"""Unit tests for SessionModel — stream-json event ingest + coalescing.

Pure-Python paths only: no subprocess, no live Qt event loop. The
session-scoped `qt_app` fixture from `conftest.py` gives us a
QCoreApplication so QObject / QAbstractListModel subclasses construct
cleanly; everything else is exercised via direct method calls and a
lightweight signal-capture helper.
"""

from __future__ import annotations

import dataclasses

import pytest

from symmetria_ide.session_models import (
    AgentRow,
    SessionModel,
    _compute_tool_diff,
    _extract_assistant_text,
    _extract_tool_result_blocks,
    _flatten_content_blocks,
    _flatten_tool_results,
    _harvest_tool_uses,
    _maybe_diff_for_tool_result,
    _row_from_result,
    _row_from_system,
    _row_from_user,
    _unified_diff,
)

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _text_delta_event(text: str, uuid: str = "u") -> dict:
    """Synthesise a content_block_delta / text_delta event.

    Shape taken verbatim from the Step 1 baseline samples — the inner
    envelope matches Anthropic's SSE frame wrapped in the CLI's
    `stream_event` type.
    """
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        },
        "uuid": uuid,
        "session_id": "s",
    }


# ---------------------------------------------------------------------------
# Model Qt surface
# ---------------------------------------------------------------------------


def test_empty_model_has_zero_rows():
    m = SessionModel()
    assert m.rowCount() == 0


def test_role_names_are_stable():
    """QML delegates bind to the byte-string role names — regression guard.

    Adding a role is fine; renaming one breaks every delegate that
    references the old name. This test is the load-bearing rename
    alarm.
    """
    m = SessionModel()
    names = m.roleNames()
    assert names[SessionModel.KindRole] == b"kind"
    assert names[SessionModel.RoleRole] == b"role"
    assert names[SessionModel.TextRole] == b"text"
    assert names[SessionModel.PartialRole] == b"partial"
    assert names[SessionModel.SubtypeRole] == b"subtype"
    assert names[SessionModel.RawRole] == b"raw"


def test_data_out_of_range_returns_none():
    m = SessionModel()
    assert m.data(m.index(0), SessionModel.TextRole) is None
    assert m.data(m.index(5), SessionModel.KindRole) is None


# ---------------------------------------------------------------------------
# apply() routing over every top-level `type`
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "expected_role", "expected_kind"),
    [
        pytest.param(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "hi"}]},
            },
            "assistant",
            "assistant",
            id="assistant_text",
        ),
        pytest.param(
            {"type": "user", "message": {"content": "hello"}},
            "user",
            "user",
            id="user_string_content",
        ),
        pytest.param(
            {"type": "system", "subtype": "init", "session_id": "abcdef12-1234"},
            "system",
            "system",
            id="system_init",
        ),
        pytest.param(
            {
                "type": "result",
                "subtype": "success",
                "duration_ms": 1234,
                "total_cost_usd": 0.05,
            },
            "system",
            "result",
            id="result_success",
        ),
        pytest.param(
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
            "system",
            "rate_limit_event",
            id="rate_limit",
        ),
        pytest.param(
            {"type": "unknown_future_type"},
            "",
            "unknown_future_type",
            id="unknown_kind_appends_empty_role_row",
        ),
    ],
)
def test_apply_appends_one_row_per_non_stream_event(
    event: dict, expected_role: str, expected_kind: str
) -> None:
    m = SessionModel()
    m.apply(event)
    assert m.rowCount() == 1
    idx = m.index(0)
    assert m.data(idx, SessionModel.RoleRole) == expected_role
    assert m.data(idx, SessionModel.KindRole) == expected_kind
    # Every non-stream-event row is final, never partial.
    assert m.data(idx, SessionModel.PartialRole) is False


# ---------------------------------------------------------------------------
# Streaming text coalescing
# ---------------------------------------------------------------------------


def test_first_text_delta_opens_a_streaming_row():
    m = SessionModel()
    m.apply(_text_delta_event("Hel"))
    assert m.rowCount() == 1
    idx = m.index(0)
    assert m.data(idx, SessionModel.PartialRole) is True
    assert m.data(idx, SessionModel.TextRole) == "Hel"
    assert m.data(idx, SessionModel.RoleRole) == "assistant"
    assert m.data(idx, SessionModel.KindRole) == "stream_event"


def test_subsequent_deltas_extend_the_same_row():
    m = SessionModel()
    m.apply(_text_delta_event("Hel"))
    m.apply(_text_delta_event("lo"))
    m.apply(_text_delta_event(", world"))
    assert m.rowCount() == 1
    assert m.data(m.index(0), SessionModel.TextRole) == "Hello, world"


def test_extension_emits_data_changed_scoped_to_text_role():
    """Gotcha #3 — scope `dataChanged` to the role that actually changed.

    Empty role lists force QML to re-evaluate every binding on the
    row; the codebase convention (CapsuleModel, CompletionModel, etc.)
    is to pass a list containing just the touched role. This test
    locks that contract in so a future "simplification" can't regress
    it silently.
    """
    m = SessionModel()
    captured: list[list[int]] = []
    m.dataChanged.connect(lambda _tl, _br, roles: captured.append(list(roles)))
    m.apply(_text_delta_event("A"))  # first delta — appends a row, no dataChanged
    m.apply(_text_delta_event("B"))  # extends — one dataChanged with TextRole
    assert captured == [[SessionModel.TextRole]]


def test_non_text_stream_event_subtypes_are_ignored():
    m = SessionModel()
    m.apply({"type": "stream_event", "event": {"type": "message_start"}})
    m.apply({"type": "stream_event", "event": {"type": "ping"}})
    m.apply(
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}
    )
    m.apply(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "input_json_delta", "partial_json": "{"},
            },
        }
    )
    assert m.rowCount() == 0


def test_empty_text_delta_is_skipped():
    m = SessionModel()
    m.apply(_text_delta_event(""))
    assert m.rowCount() == 0


def test_assistant_event_finalises_streaming_row_in_place():
    """Multi-turn duplicate-row regression guard.

    The streaming row built up from `text_delta` frames must be
    REPLACED in place when the canonical `assistant` event arrives —
    NOT left dangling alongside a fresh appended row. Showing both
    looked like Claude was responding twice (a real user-reported bug
    during multi-turn testing).

    Re-extracting from the canonical content blocks also fills in
    `[tool: name]` markers that never streamed as deltas; that's why
    we don't just keep the partial-buffered text and flip the flag.
    """
    m = SessionModel()
    # Capture dataChanged emissions during finalization (gotcha #3 guard:
    # scoped role lists let QML skip re-evaluating unchanged bindings).
    finalize_roles: list[list[int]] = []
    m.dataChanged.connect(lambda _tl, _br, roles: finalize_roles.append(list(roles)))
    m.apply(_text_delta_event("partial"))
    # The first delta opens a new row — that's beginInsertRows, not dataChanged.
    assert finalize_roles == [], "opening a new streaming row must not emit dataChanged"
    m.apply(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Let me check. "},
                    {"type": "tool_use", "name": "Read", "input": {"path": "x"}},
                    {"type": "text", "text": " Answer: 4."},
                ]
            },
        }
    )
    # ONE row, finalised in place. The streaming row is now the
    # canonical assistant row.
    assert m.rowCount() == 1
    row = m.index(0)
    assert m.data(row, SessionModel.PartialRole) is False
    assert m.data(row, SessionModel.KindRole) == "assistant"
    assert m.data(row, SessionModel.RoleRole) == "assistant"
    # Text comes from re-extraction of the canonical content blocks,
    # so tool_use is included even though it never streamed.
    assert m.data(row, SessionModel.TextRole) == "Let me check. [tool: Read] Answer: 4."
    # Finalization must emit exactly one scoped dataChanged (gotcha #3).
    # RoleRole is intentionally absent — role stays "assistant" so QML
    # doesn't re-evaluate the colour-map binding unnecessarily.
    assert len(finalize_roles) == 1, "finalization must emit exactly one dataChanged"
    emitted = set(finalize_roles[0])
    assert SessionModel.KindRole in emitted
    assert SessionModel.TextRole in emitted
    assert SessionModel.PartialRole in emitted
    assert SessionModel.SubtypeRole in emitted
    assert SessionModel.RawRole in emitted
    assert SessionModel.RoleRole not in emitted, (
        "RoleRole must NOT be in dataChanged — role does not change during finalization"
    )


def test_assistant_event_without_prior_streaming_appends_canonical_row():
    """Fallback path: a non-streamed assistant event still produces a row."""
    m = SessionModel()
    m.apply(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hi"}]},
        }
    )
    assert m.rowCount() == 1
    row = m.index(0)
    assert m.data(row, SessionModel.PartialRole) is False
    assert m.data(row, SessionModel.TextRole) == "hi"


def test_next_stream_after_boundary_starts_fresh_row():
    m = SessionModel()
    m.apply(_text_delta_event("turn1-a"))
    m.apply(_text_delta_event("turn1-b"))
    # Non-streaming event resets the coalesce — the next text_delta
    # opens a brand-new streaming row, not an extension of turn 1.
    m.apply({"type": "system", "subtype": "init", "session_id": "s"})
    m.apply(_text_delta_event("turn2-a"))
    assert m.rowCount() == 3
    assert m.data(m.index(0), SessionModel.TextRole) == "turn1-aturn1-b"
    assert m.data(m.index(2), SessionModel.TextRole) == "turn2-a"


# ---------------------------------------------------------------------------
# Row translator helpers
# ---------------------------------------------------------------------------


def test_extract_assistant_text_joins_text_blocks_and_marks_tool_use():
    message = {
        "content": [
            {"type": "text", "text": "Let me check. "},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "x"}},
            {"type": "text", "text": " Answer: 4."},
        ]
    }
    assert _extract_assistant_text(message) == ("Let me check. [tool: Read] Answer: 4.")


def test_extract_assistant_text_handles_missing_or_bad_content():
    assert _extract_assistant_text({}) == ""
    assert _extract_assistant_text({"content": None}) == ""
    # Non-list content (shape variation) must not crash.
    assert _extract_assistant_text({"content": "should be a list"}) == ""


def test_flatten_content_blocks_skips_unknown_types():
    # Future content-block type contributes nothing rather than
    # raising — surfaces as a visible gap to notice, not a crash.
    assert _flatten_content_blocks([{"type": "future_unknown"}]) == ""
    # Non-dict entries are silently skipped.
    assert _flatten_content_blocks([None, 42, "string"]) == ""
    # tool_result renders a marker.
    assert "[tool result]" in _flatten_content_blocks(
        [{"type": "tool_result", "content": []}]
    )


def test_row_from_system_summarises_init_and_hooks():
    init_row = _row_from_system(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "abcd1234-5678",
        }
    )
    assert "abcd1234" in init_row.text

    hook_row = _row_from_system(
        {
            "type": "system",
            "subtype": "hook_started",
            "hook_name": "SessionStart:startup",
        }
    )
    assert "hook started" in hook_row.text
    assert "SessionStart:startup" in hook_row.text

    hook_outcome_row = _row_from_system(
        {
            "type": "system",
            "subtype": "hook_response",
            "hook_name": "SessionStart:startup",
            "outcome": "success",
        }
    )
    assert "success" in hook_outcome_row.text


def test_row_from_result_renders_duration_and_cost():
    row = _row_from_result(
        {"type": "result", "duration_ms": 2720, "total_cost_usd": 0.299105}
    )
    assert "2720ms" in row.text
    assert "$0.2991" in row.text


def test_row_from_result_tolerates_missing_fields():
    row = _row_from_result({"type": "result"})
    # Keeps the "done" prefix even when duration / cost are absent.
    assert row.text == "done"


def test_row_from_user_handles_list_content_too():
    row = _row_from_user(
        {"type": "user", "message": {"content": [{"type": "text", "text": "hello"}]}}
    )
    assert row.role == "user"
    assert row.text == "hello"


# ---------------------------------------------------------------------------
# tool_result routing — Anthropic's protocol overloads the `user` role.
# Tool results arrive in `user` envelopes and must NOT render as user echoes;
# they should appear as their own machine-output rows. The sidecar's content-
# aware filter passes them through; SessionModel disambiguates via content
# shape.
# ---------------------------------------------------------------------------


def test_row_from_user_with_tool_result_block_renders_as_tool_role():
    row = _row_from_user(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_001",
                        "content": "The file /home/jc/foo.md has been updated.",
                    }
                ]
            },
        }
    )
    assert row.kind == "tool_result"
    assert row.role == "tool"
    assert "/home/jc/foo.md has been updated" in row.text
    assert row.subtype == ""  # not flagged as error


def test_row_from_user_with_error_tool_result_marks_subtype_error():
    row = _row_from_user(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_002",
                        "content": "File not found: /tmp/missing.txt",
                        "is_error": True,
                    }
                ]
            },
        }
    )
    assert row.kind == "tool_result"
    assert row.role == "tool"
    assert row.subtype == "error"
    assert "File not found" in row.text


def test_row_from_user_with_tool_result_inner_content_blocks_flattens_text():
    """tool_result.content can itself be a list of inner blocks (image+text)."""
    row = _row_from_user(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_003",
                        "content": [
                            {"type": "text", "text": "ls output:"},
                            {"type": "text", "text": "\nfoo.md\nbar.py"},
                        ],
                    }
                ]
            },
        }
    )
    assert row.role == "tool"
    assert "ls output:" in row.text
    assert "foo.md" in row.text


def test_row_from_user_with_multiple_tool_results_concatenates_with_blank_line():
    """Rare shape — usually one tool_result per envelope, but the protocol allows many."""
    row = _row_from_user(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_a",
                        "content": "first result",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_b",
                        "content": "second result",
                    },
                ]
            },
        }
    )
    assert row.role == "tool"
    assert row.text == "first result\n\nsecond result"


def test_row_from_user_pure_text_still_renders_as_user_defensively():
    """Sidecar drops these, but if one ever leaks through it should still look right."""
    row = _row_from_user({"type": "user", "message": {"content": "hello"}})
    assert row.kind == "user"
    assert row.role == "user"
    assert row.text == "hello"


def test_apply_routes_user_tool_result_through_apply_path():
    """End-to-end: apply(user-with-tool-result) lands a tool row in the model."""
    m = SessionModel()
    m.apply(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_x",
                        "content": "edit applied",
                    }
                ]
            },
        }
    )
    assert m.rowCount() == 1
    assert m.data(m.index(0), SessionModel.RoleRole) == "tool"
    assert m.data(m.index(0), SessionModel.KindRole) == "tool_result"
    assert m.data(m.index(0), SessionModel.TextRole) == "edit applied"


# ---------------------------------------------------------------------------
# Row value object — invariants
# ---------------------------------------------------------------------------


def test_agent_row_is_frozen():
    row = AgentRow(kind="x", role="", text="", partial=False, subtype="", raw={})
    # The specific exception, not a blind `Exception`: naming it is what makes
    # this assert prove the dataclass is FROZEN rather than merely broken.
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.text = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_clear_resets_rows_and_streaming_state():
    m = SessionModel()
    m.apply(_text_delta_event("partial"))
    m.apply(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "done"}]},
        }
    )
    # One row — the streaming row was finalised in place by the
    # assistant event (not duplicated as a second row).
    assert m.rowCount() == 1
    m.clear()
    assert m.rowCount() == 0
    # After clear, the next stream delta opens a fresh streaming row.
    m.apply(_text_delta_event("next"))
    assert m.rowCount() == 1


def test_clear_is_noop_when_already_empty():
    m = SessionModel()
    m.clear()
    assert m.rowCount() == 0


def test_clear_resets_tool_use_cache():
    """Cache survives clear() only if clear() resets it — regression guard for the
    fix that added `self._tool_use_cache = {}` to `clear()`.

    If clear() does NOT reset the cache, the tool_use_id from the previous
    session lingers; a new session's user tool_result could then match it and
    produce a spurious diff row from a prior session's data.
    """
    m = SessionModel()
    # Populate the cache via an assistant event.
    m.apply(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_session1",
                        "name": "Edit",
                        "input": {
                            "file_path": "/tmp/x.py",
                            "old_string": "old",
                            "new_string": "new",
                        },
                    }
                ]
            },
        }
    )
    # Reset the model (simulates starting a fresh session in-place).
    m.clear()
    assert m.rowCount() == 0

    # If the cache was correctly cleared, a tool_result referencing the
    # same tool_use_id should fall back to kind="tool_result", not "tool_diff".
    m.apply(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_session1",
                        "content": "File has been updated",
                    }
                ]
            },
        }
    )
    assert m.rowCount() == 1
    assert m.data(m.index(0), SessionModel.KindRole) == "tool_result"


def test_on_host_closed_emits_host_closed_signal_once():
    m = SessionModel()
    received: list[int] = []
    m.hostClosed.connect(lambda: received.append(1))
    m.on_host_closed()
    assert received == [1]


# ---------------------------------------------------------------------------
# Permission row routing — sidecar canUseTool integration
# ---------------------------------------------------------------------------
#
# `permission_request` envelopes are sidecar-synthesized — they don't
# exist in raw stream-json. SessionModel routes them into a pending
# row carrying request_id + permission_state="pending"; the
# `resolve_permission` slot mutates that state to "approved"/"denied"
# when the user clicks the inline card. These tests pin the dispatch
# routing AND the scoped-role-list discipline (gotcha #3 — empty role
# lists force a full re-bind; we want exactly one role).


def _permission_request_event(request_id: str, tool: str, title: str = "") -> dict:
    return {
        "type": "permission_request",
        "request_id": request_id,
        "tool_name": tool,
        "tool_use_id": "tu-" + request_id,
        "input": {},
        "title": title,
        "note": "sidecar-synthesized",
    }


def test_permission_request_event_appends_pending_row():
    m = SessionModel()
    m.apply(_permission_request_event("abc", "Bash", "Allow Bash?"))
    assert m.rowCount() == 1
    idx = m.index(0)
    assert m.data(idx, SessionModel.KindRole) == "permission_request"
    assert m.data(idx, SessionModel.RoleRole) == "system"
    assert m.data(idx, SessionModel.PermissionStateRole) == "pending"
    assert m.data(idx, SessionModel.RequestIdRole) == "abc"
    # When `title` is present, body text uses it verbatim — matches the
    # SDK's CanUseTool docstring guidance.
    assert m.data(idx, SessionModel.TextRole) == "Allow Bash?"
    # subtype carries the tool name so the QML card can label without
    # re-deriving from the body text.
    assert m.data(idx, SessionModel.SubtypeRole) == "Bash"


def test_permission_request_falls_back_to_tool_name_when_no_title():
    m = SessionModel()
    m.apply(_permission_request_event("abc", "Read"))
    idx = m.index(0)
    assert m.data(idx, SessionModel.TextRole) == "Allow Read?"


def test_resolve_permission_allow_flips_to_approved_with_scoped_dataChanged():
    m = SessionModel()
    m.apply(_permission_request_event("abc", "Edit"))

    # Capture (top-left, bottom-right, role list) for every dataChanged.
    received: list[tuple[int, int, list[int]]] = []
    m.dataChanged.connect(
        lambda tl, br, roles: received.append((tl.row(), br.row(), list(roles)))
    )

    m.resolve_permission("abc", "allow")

    assert m.data(m.index(0), SessionModel.PermissionStateRole) == "approved"
    # Exactly one emission, scoped to the single role that changed.
    # Empty role list would force every binding on every visible
    # delegate to re-evaluate — gotcha #3 invariant.
    assert received == [(0, 0, [SessionModel.PermissionStateRole])]


def test_resolve_permission_deny_flips_to_denied():
    m = SessionModel()
    m.apply(_permission_request_event("abc", "Edit"))
    m.resolve_permission("abc", "deny")
    assert m.data(m.index(0), SessionModel.PermissionStateRole) == "denied"


def test_resolve_permission_unknown_id_is_a_noop():
    """Race: user clicks deny just as host crashes → resolve fires with
    an id that no longer matches a pending row. Must NOT mutate state
    or emit dataChanged."""
    m = SessionModel()
    m.apply(_permission_request_event("abc", "Edit"))

    received: list[int] = []
    m.dataChanged.connect(lambda tl, br, roles: received.append(1))

    m.resolve_permission("never-existed", "allow")

    assert m.data(m.index(0), SessionModel.PermissionStateRole) == "pending"
    assert received == []


def test_resolve_permission_skips_already_resolved_rows():
    """Two consecutive resolves must NOT re-flip an already-resolved row.

    Otherwise repeated clicks during a stuck UI cycle could spuriously
    re-emit dataChanged, forcing pointless re-binds across every
    visible delegate. The scan walks only `permission_state == "pending"`
    rows by design.
    """
    m = SessionModel()
    m.apply(_permission_request_event("abc", "Edit"))
    m.resolve_permission("abc", "allow")

    received: list[int] = []
    m.dataChanged.connect(lambda tl, br, roles: received.append(1))

    m.resolve_permission("abc", "deny")

    # Still approved (first resolve wins). No second emission.
    assert m.data(m.index(0), SessionModel.PermissionStateRole) == "approved"
    assert received == []


def test_role_names_includes_permission_state_and_request_id():
    """Regression guard — QML delegates bind to byte-string role names.

    A rename here breaks the typed-property delegate's
    `required property string permissionState` / `requestId` bindings.
    """
    names = SessionModel().roleNames()
    assert names[SessionModel.PermissionStateRole] == b"permissionState"
    assert names[SessionModel.RequestIdRole] == b"requestId"


def test_permission_request_falls_back_to_generic_text_when_no_title_and_no_tool_name():
    """Third fallback: neither title nor tool_name present → 'Allow tool call?'.

    Exercises the bottom-most branch in _row_from_permission_request's
    text-construction logic. A sidecar event with an empty tool_name (e.g.
    an unexpected SDK event shape) must still produce a readable row rather
    than an empty body.
    """
    m = SessionModel()
    m.apply(
        {
            "type": "permission_request",
            "request_id": "no-tool",
            "tool_name": "",
            "tool_use_id": "tu-x",
            "input": {},
            "note": "sidecar-synthesized",
        }
    )
    idx = m.index(0)
    assert m.data(idx, SessionModel.TextRole) == "Allow tool call?"
    assert m.data(idx, SessionModel.SubtypeRole) == ""


# ---------------------------------------------------------------------------
# Direct coverage for _extract_tool_result_blocks and _flatten_tool_results
# These helpers are exercised indirectly through _row_from_user above, but
# direct tests anchor edge cases that _row_from_user doesn't reach.
# ---------------------------------------------------------------------------


def test_extract_tool_result_blocks_returns_empty_for_non_list_content():
    assert _extract_tool_result_blocks(None) == []
    assert _extract_tool_result_blocks("string") == []
    assert _extract_tool_result_blocks(42) == []


def test_extract_tool_result_blocks_returns_empty_for_empty_list():
    assert _extract_tool_result_blocks([]) == []


def test_extract_tool_result_blocks_filters_non_dict_entries():
    assert _extract_tool_result_blocks([None, 42, "str"]) == []


def test_extract_tool_result_blocks_returns_only_tool_result_typed_blocks():
    content = [
        {"type": "text", "text": "hi"},
        {"type": "tool_result", "content": "ok"},
        {"type": "tool_use", "name": "Read"},
    ]
    result = _extract_tool_result_blocks(content)
    assert len(result) == 1
    assert result[0]["type"] == "tool_result"


def test_flatten_tool_results_empty_list_returns_empty_string():
    text, is_error = _flatten_tool_results([])
    assert text == ""
    assert is_error is False


def test_flatten_tool_results_string_content():
    text, is_error = _flatten_tool_results(
        [{"type": "tool_result", "content": "hello"}]
    )
    assert text == "hello"
    assert is_error is False


def test_flatten_tool_results_list_content_flattens_inner_text_blocks():
    text, is_error = _flatten_tool_results(
        [
            {
                "type": "tool_result",
                "content": [
                    {"type": "text", "text": "line 1"},
                    {"type": "text", "text": "line 2"},
                ],
            }
        ]
    )
    assert "line 1" in text
    assert "line 2" in text
    assert is_error is False


def test_flatten_tool_results_missing_content_field_is_empty():
    """Block missing 'content' key entirely — not str, not list, skipped."""
    text, is_error = _flatten_tool_results([{"type": "tool_result"}])
    assert text == ""
    assert is_error is False


def test_flatten_tool_results_none_content_is_empty():
    """Block with explicit content=None — not str, not list, skipped."""
    text, is_error = _flatten_tool_results([{"type": "tool_result", "content": None}])
    assert text == ""
    assert is_error is False


def test_flatten_tool_results_non_str_non_list_content_is_empty():
    """Block with unexpected content type (e.g. int) — skipped gracefully."""
    text, is_error = _flatten_tool_results([{"type": "tool_result", "content": 42}])
    assert text == ""
    assert is_error is False


def test_flatten_tool_results_is_error_propagates_even_when_content_empty():
    """is_error=True must set the flag even when the content string is empty."""
    text, is_error = _flatten_tool_results(
        [{"type": "tool_result", "content": "", "is_error": True}]
    )
    assert text == ""
    assert is_error is True


def test_flatten_tool_results_joins_multiple_blocks_skipping_empty():
    """Multiple blocks: empty string parts are excluded from the join."""
    text, is_error = _flatten_tool_results(
        [
            {"type": "tool_result", "content": "a"},
            {"type": "tool_result", "content": ""},  # empty — skipped in join
            {"type": "tool_result", "content": "b"},
        ]
    )
    assert text == "a\n\nb"
    assert is_error is False


# ---------------------------------------------------------------------------
# _row_from_user robustness — null/missing message field
# ---------------------------------------------------------------------------


def test_row_from_user_with_null_message_is_safe():
    """message=None is guarded by `event.get('message') or {}` — must not raise."""
    row = _row_from_user({"type": "user", "message": None})
    assert row.role == "user"
    assert row.text == ""


def test_row_from_user_with_missing_message_key_is_safe():
    """No message key at all — same guard applies."""
    row = _row_from_user({"type": "user"})
    assert row.role == "user"
    assert row.text == ""


# ---------------------------------------------------------------------------
# Mixed-envelope edge case: text blocks + tool_result blocks in same message.
# Protocol-wise this shouldn't occur, but the predicate in containsToolResult
# (sidecar) + _extract_tool_result_blocks (Python) agree: the envelope is
# treated as a tool-result envelope and only the tool_result content surfaces.
# ---------------------------------------------------------------------------


def test_row_from_user_with_mixed_text_and_tool_result_blocks():
    """Mixed envelope: tool_result wins; text blocks in the same list are discarded.

    This matches what containsToolResult (sidecar) + _extract_tool_result_blocks
    (Python) agree on: once any tool_result block is found, the envelope is a
    tool-result envelope. Text blocks at the top level are discarded so the row
    doesn't conflate a user comment with a tool response.
    """
    row = _row_from_user(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "text", "text": "this is a user comment"},
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_x",
                        "content": "tool output",
                    },
                ]
            },
        }
    )
    assert row.kind == "tool_result"
    assert row.role == "tool"
    assert "tool output" in row.text
    # Text block content is discarded — only tool_result content surfaces.
    assert "user comment" not in row.text


# ---------------------------------------------------------------------------
# Tool-use cache + diff rendering — Stage 1 of Edit/Write diff visualization
# ---------------------------------------------------------------------------
#
# These tests cover the new pipeline that turns Edit/Write/MultiEdit/
# NotebookEdit tool_results into rich `tool_diff` rows: harvesting tool_use
# blocks from assistant messages, looking them up by `tool_use_id` when the
# matching `user`-role tool_result lands, and computing unified diffs via
# stdlib `difflib`.


def test_harvest_tool_uses_extracts_id_and_input_from_assistant_blocks():
    cache: dict[str, dict] = {}
    _harvest_tool_uses(
        {
            "content": [
                {"type": "text", "text": "Editing the file"},
                {
                    "type": "tool_use",
                    "id": "toolu_abc",
                    "name": "Edit",
                    "input": {
                        "file_path": "/tmp/foo.py",
                        "old_string": "x = 1",
                        "new_string": "x = 2",
                    },
                },
            ]
        },
        cache,
    )
    assert "toolu_abc" in cache
    assert cache["toolu_abc"]["name"] == "Edit"
    assert cache["toolu_abc"]["input"]["old_string"] == "x = 1"


def test_harvest_tool_uses_skips_blocks_missing_id():
    """Defensive: block without an id is unusable for pairing — just skip."""
    cache: dict[str, dict] = {}
    _harvest_tool_uses(
        {"content": [{"type": "tool_use", "name": "Edit", "input": {}}]},
        cache,
    )
    assert cache == {}


def test_harvest_tool_uses_handles_non_list_content_gracefully():
    cache: dict[str, dict] = {}
    _harvest_tool_uses({"content": None}, cache)
    _harvest_tool_uses({"content": "string"}, cache)
    _harvest_tool_uses({}, cache)
    assert cache == {}


def test_harvest_tool_uses_normalizes_missing_input_to_empty_dict():
    """Missing/non-dict input becomes {} so downstream lookups don't crash."""
    cache: dict[str, dict] = {}
    _harvest_tool_uses(
        {"content": [{"type": "tool_use", "id": "t1", "name": "Bash"}]},
        cache,
    )
    assert cache["t1"]["input"] == {}


# --- _unified_diff ----------------------------------------------------


def test_unified_diff_basic_replace():
    diff = _unified_diff("hello\nworld\n", "hello\nplanet\n", "/tmp/x.txt")
    assert "--- /tmp/x.txt" in diff
    assert "+++ /tmp/x.txt" in diff
    assert "-world" in diff
    assert "+planet" in diff


def test_unified_diff_returns_empty_for_identical_inputs():
    """No changes → no diff. We use this in _maybe_diff_for_tool_result to fall back."""
    assert _unified_diff("same\n", "same\n", "/tmp/x.txt") == ""


def test_unified_diff_lineterm_strips_trailing_newlines():
    """`lineterm=""` keeps lines clean for QML's per-line tinting."""
    diff = _unified_diff("a\nb\n", "a\nc\n", "/tmp/x")
    # No line in the diff should end with extra whitespace.
    for line in diff.split("\n"):
        assert line == line.rstrip()


# --- _compute_tool_diff ----------------------------------------------


def test_compute_tool_diff_edit_renders_old_to_new():
    diff = _compute_tool_diff(
        "Edit",
        {
            "file_path": "/tmp/a.py",
            "old_string": "x = 1",
            "new_string": "x = 2",
        },
    )
    assert diff is not None
    assert "-x = 1" in diff
    assert "+x = 2" in diff


def test_compute_tool_diff_write_uses_empty_string_as_before():
    """Write replaces the entire file — visual is 'every line added'."""
    diff = _compute_tool_diff(
        "Write",
        {"file_path": "/tmp/new.txt", "content": "first\nsecond\n"},
    )
    assert diff is not None
    assert "+first" in diff
    assert "+second" in diff
    # Nothing was removed.
    assert "-first" not in diff


def test_compute_tool_diff_multiedit_concatenates_per_edit_diffs():
    diff = _compute_tool_diff(
        "MultiEdit",
        {
            "file_path": "/tmp/x.py",
            "edits": [
                {"old_string": "a", "new_string": "A"},
                {"old_string": "b", "new_string": "B"},
            ],
        },
    )
    assert diff is not None
    assert "-a" in diff
    assert "+A" in diff
    assert "-b" in diff
    assert "+B" in diff


def test_compute_tool_diff_multiedit_skips_malformed_entries():
    """Per-edit malformed dicts get dropped, not crash the whole MultiEdit."""
    diff = _compute_tool_diff(
        "MultiEdit",
        {
            "file_path": "/tmp/x",
            "edits": [
                {"old_string": "a", "new_string": "A"},
                {"old_string": 42, "new_string": "B"},  # malformed
                {"old_string": "c", "new_string": "C"},
            ],
        },
    )
    assert diff is not None
    assert "+A" in diff
    assert "+C" in diff
    assert "+B" not in diff


def test_compute_tool_diff_notebookedit_falls_back_to_empty_old_source():
    """NotebookEdit may omit old_source on cell creation — treat as empty."""
    diff = _compute_tool_diff(
        "NotebookEdit",
        {"notebook_path": "/tmp/n.ipynb", "new_source": "import numpy"},
    )
    assert diff is not None
    assert "+import numpy" in diff


def test_compute_tool_diff_returns_none_for_unknown_tool():
    assert _compute_tool_diff("Bash", {"command": "ls"}) is None


def test_compute_tool_diff_returns_none_for_malformed_edit_input():
    """Type-mismatched input → None, lets caller fall back to plain text."""
    assert _compute_tool_diff("Edit", {"old_string": 42, "new_string": "x"}) is None
    assert _compute_tool_diff("Edit", {"file_path": "/tmp/x"}) is None


# --- _maybe_diff_for_tool_result -------------------------------------


def test_maybe_diff_for_tool_result_succeeds_for_cached_edit():
    cache = {
        "toolu_1": {
            "name": "Edit",
            "input": {"file_path": "/tmp/x", "old_string": "a", "new_string": "b"},
        }
    }
    blocks = [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]
    diff_text, tool_name = _maybe_diff_for_tool_result(blocks, cache)
    assert diff_text is not None
    assert "-a" in diff_text
    assert "+b" in diff_text
    assert tool_name == "Edit"


def test_maybe_diff_for_tool_result_returns_none_when_cache_misses():
    """Cache miss (e.g. assistant message arrived before sidecar started) → no diff."""
    blocks = [{"type": "tool_result", "tool_use_id": "unknown", "content": "ok"}]
    diff_text, tool_name = _maybe_diff_for_tool_result(blocks, {})
    assert diff_text is None
    assert tool_name == ""


def test_maybe_diff_for_tool_result_returns_none_for_error_block():
    """Errored results carry the error message in `content`, not a diff."""
    cache = {
        "toolu_1": {
            "name": "Edit",
            "input": {"file_path": "/tmp/x", "old_string": "a", "new_string": "b"},
        }
    }
    blocks = [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": "no match found",
            "is_error": True,
        }
    ]
    assert _maybe_diff_for_tool_result(blocks, cache) == (None, "")


def test_maybe_diff_for_tool_result_returns_none_for_non_diff_tool():
    """Bash / Read / Grep results are not diff-able — fall through."""
    cache = {"toolu_1": {"name": "Bash", "input": {"command": "ls"}}}
    blocks = [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]
    assert _maybe_diff_for_tool_result(blocks, cache) == (None, "")


def test_maybe_diff_for_tool_result_returns_none_for_multiple_blocks():
    """Multi-block envelopes are rare — render plainly to avoid mislabelling."""
    cache = {
        "toolu_1": {
            "name": "Edit",
            "input": {"file_path": "/tmp/x", "old_string": "a", "new_string": "b"},
        }
    }
    blocks = [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"},
        {"type": "tool_result", "tool_use_id": "toolu_2", "content": "also ok"},
    ]
    assert _maybe_diff_for_tool_result(blocks, cache) == (None, "")


# --- _row_from_user with cache → tool_diff row -----------------------


def test_row_from_user_with_cached_edit_emits_tool_diff_row():
    cache = {
        "toolu_1": {
            "name": "Edit",
            "input": {
                "file_path": "/tmp/x.py",
                "old_string": "x = 1",
                "new_string": "x = 2",
            },
        }
    }
    row = _row_from_user(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "File has been updated",
                    }
                ]
            },
        },
        tool_use_cache=cache,
    )
    assert row.kind == "tool_diff"
    assert row.role == "tool"
    assert row.subtype == "Edit"  # tool name carried for the QML role label
    assert "-x = 1" in row.text
    assert "+x = 2" in row.text


def test_row_from_user_falls_back_to_tool_result_when_cache_miss():
    """Without a matching cache entry, the existing plain `tool_result` path runs."""
    row = _row_from_user(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "missing",
                        "content": "File has been updated",
                    }
                ]
            },
        },
        tool_use_cache={},
    )
    assert row.kind == "tool_result"
    assert row.text == "File has been updated"


# --- End-to-end via apply() with the model's own cache ---------------


def test_apply_assistant_then_user_emits_tool_diff_row():
    """Full pipeline: assistant tool_use lands → cache populated → user tool_result
    arrives → SessionModel emits a `tool_diff` row instead of plain `tool_result`.
    This is the user-visible promise of Stage 1."""
    m = SessionModel()
    m.apply(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Editing now"},
                    {
                        "type": "tool_use",
                        "id": "toolu_xyz",
                        "name": "Edit",
                        "input": {
                            "file_path": "/tmp/foo.py",
                            "old_string": "old",
                            "new_string": "new",
                        },
                    },
                ]
            },
        }
    )
    m.apply(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_xyz",
                        "content": "File has been updated",
                    }
                ]
            },
        }
    )
    # row 0 = assistant; row 1 = tool_diff (not tool_result)
    assert m.rowCount() == 2
    assert m.data(m.index(1), SessionModel.KindRole) == "tool_diff"
    assert m.data(m.index(1), SessionModel.SubtypeRole) == "Edit"
    text = m.data(m.index(1), SessionModel.TextRole)
    assert "-old" in text
    assert "+new" in text
