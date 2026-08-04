"""Unit tests for `parse_jsonl_line` — the pure JSONL parser.

No subprocess. No threads. No Qt event loop. The parser is extracted
from `SessionHost._run_stdout_loop` as a free function so we can
cover every malformed-input edge case without orchestrating a live
subprocess. Lifecycle testing lives outside this file — it needs a live sidecar
process with a valid SDK session and belongs in the integration suite.
"""

from __future__ import annotations

from symmetria_ide.session_host import parse_jsonl_line

# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_well_formed_event_is_parsed():
    line = '{"type": "assistant", "uuid": "abc"}\n'
    out = parse_jsonl_line(line)
    assert out == {"type": "assistant", "uuid": "abc"}


def test_trailing_and_leading_whitespace_is_stripped():
    line = '   \t{"type": "result"}  \n'
    out = parse_jsonl_line(line)
    assert out == {"type": "result"}


def test_utf8_payload_round_trips():
    # Real streams carry non-ASCII content (em-dashes, emoji in
    # user-supplied text, etc.). Make sure the parser doesn't mangle
    # them.
    line = '{"type": "user", "message": {"content": "¡hola! 🎉 — ok"}}\n'
    out = parse_jsonl_line(line)
    assert out is not None
    assert out["message"]["content"] == "¡hola! 🎉 — ok"


def test_nested_objects_are_preserved():
    line = (
        '{"type": "stream_event", "event": '
        '{"type": "content_block_delta", '
        '"delta": {"type": "text_delta", "text": "hi"}}}\n'
    )
    out = parse_jsonl_line(line)
    assert out is not None
    assert out["event"]["delta"]["text"] == "hi"


# ---------------------------------------------------------------------------
# Sad path — must never crash, must return None
# ---------------------------------------------------------------------------


def test_blank_line_returns_none():
    assert parse_jsonl_line("") is None
    assert parse_jsonl_line("\n") is None
    assert parse_jsonl_line("   \t   \n") is None


def test_malformed_json_returns_none():
    # Unterminated brace — typical partial-line read symptom if
    # buffering ever fails us.
    assert parse_jsonl_line('{"type": "assistant"\n') is None
    # Syntactically invalid.
    assert parse_jsonl_line("not json at all\n") is None
    # Truncated in the middle of a string.
    assert parse_jsonl_line('{"type": "res\n') is None


def test_json_that_isnt_an_object_returns_none():
    # Protocol contract is one object per line. Arrays, bare scalars,
    # null — all should be dropped, not converted.
    assert parse_jsonl_line("[1, 2, 3]\n") is None
    assert parse_jsonl_line("42\n") is None
    assert parse_jsonl_line('"just a string"\n') is None
    assert parse_jsonl_line("null\n") is None
    assert parse_jsonl_line("true\n") is None


def test_bom_prefix_is_tolerated():
    # BOM is never emitted by the sidecar in practice, but we strip it
    # defensively so a future change can't silently wedge us.
    line = "\ufeff" + '{"type": "assistant"}' + "\n"
    out = parse_jsonl_line(line)
    assert out == {"type": "assistant"}


def test_very_long_line_parses():
    # Long text_delta lines do happen on rich assistant output; make
    # sure the parser doesn't cap or split them.
    long_text = "x" * 50_000
    line = f'{{"type": "stream_event", "text": "{long_text}"}}\n'
    out = parse_jsonl_line(line)
    assert out is not None
    assert out["text"] == long_text
    assert len(out["text"]) == 50_000
