"""Unit tests for the shared JSONL line-framing primitives.

Covers both ``parse_jsonl_line`` (the lenient parser) and
``encode_jsonl_line`` (the serialiser), including the round-trip
contract: ``parse(encode(obj)) == obj``.

These test the module directly — not via ``session_host``'s re-export
— so a future import-path refactor doesn't silently break the
primitives without a test failure.
"""

from __future__ import annotations

import json

import pytest

from symmetria_ide.jsonl_transport import encode_jsonl_line, parse_jsonl_line

# ---------------------------------------------------------------------------
# encode_jsonl_line
# ---------------------------------------------------------------------------


def test_encode_appends_newline():
    line = encode_jsonl_line({"type": "ping"})
    assert line.endswith("\n")


def test_encode_produces_valid_json_object():
    line = encode_jsonl_line({"k": "v", "n": 1})
    obj = json.loads(line.strip())
    assert obj == {"k": "v", "n": 1}


def test_encode_ensure_ascii_true_escapes_non_ascii():
    line = encode_jsonl_line({"msg": "¡hola!"}, ensure_ascii=True)
    assert "\\u" in line  # non-ASCII is escaped
    assert "¡" not in line


def test_encode_ensure_ascii_false_keeps_non_ascii_literal():
    line = encode_jsonl_line({"msg": "¡hola! 🎉"}, ensure_ascii=False)
    assert "¡" in line
    assert "🎉" in line
    assert line.endswith("\n")


def test_encode_default_is_ensure_ascii_true():
    """The default matches json.dumps — safe for the sidecar command stream."""
    line_default = encode_jsonl_line({"x": "é"})
    line_ascii = encode_jsonl_line({"x": "é"}, ensure_ascii=True)
    assert line_default == line_ascii


# ---------------------------------------------------------------------------
# round-trip: parse(encode(obj)) == obj
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "obj",
    [
        {"type": "ping"},
        {"type": "assistant", "content": "hello"},
        {"nested": {"a": 1}, "arr": [1, 2, 3]},
        {"unicode": "¡hola! 🎉 — ok"},
    ],
    ids=["simple", "string-value", "nested-array", "unicode"],
)
def test_round_trip(obj: dict) -> None:
    """parse_jsonl_line(encode_jsonl_line(obj)) must recover the original object."""
    line = encode_jsonl_line(obj, ensure_ascii=False)
    recovered = parse_jsonl_line(line)
    assert recovered == obj


# ---------------------------------------------------------------------------
# parse_jsonl_line — re-test core cases against the module directly
# (test_session_host_parser.py tests these via the session_host re-export;
# these verify the module itself, so a rename doesn't leave both paths broken)
# ---------------------------------------------------------------------------


def test_parse_blank_returns_none():
    assert parse_jsonl_line("") is None
    assert parse_jsonl_line("\n") is None
    assert parse_jsonl_line("   ") is None


def test_parse_malformed_returns_none():
    assert parse_jsonl_line("{not json}\n") is None


def test_parse_non_object_returns_none():
    assert parse_jsonl_line("[1, 2]\n") is None
    assert parse_jsonl_line("42\n") is None


def test_parse_bom_tolerance():
    line = "﻿" + '{"type": "ok"}' + "\n"
    assert parse_jsonl_line(line) == {"type": "ok"}
