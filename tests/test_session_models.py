"""Unit tests for SessionModel — stream-json event ingest + coalescing.

Pure-Python paths only: no subprocess, no live Qt event loop. The
session-scoped `qt_app` fixture from `conftest.py` gives us a
QCoreApplication so QObject / QAbstractListModel subclasses construct
cleanly; everything else is exercised via direct method calls and a
lightweight signal-capture helper.
"""

from __future__ import annotations

import pytest

from symmetria_ide.session_models import (
    AgentRow,
    SessionModel,
    _extract_assistant_text,
    _flatten_content_blocks,
    _row_from_result,
    _row_from_system,
    _row_from_user,
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
    m.apply(_text_delta_event("partial"))
    assert m.rowCount() == 1  # streaming row is open
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
# Row value object — invariants
# ---------------------------------------------------------------------------


def test_agent_row_is_frozen():
    row = AgentRow(kind="x", role="", text="", partial=False, subtype="", raw={})
    with pytest.raises(Exception):  # frozen dataclass raises FrozenInstanceError
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


def test_on_host_closed_emits_host_closed_signal_once():
    m = SessionModel()
    received: list[int] = []
    m.hostClosed.connect(lambda: received.append(1))
    m.on_host_closed()
    assert received == [1]
