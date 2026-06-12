"""Pure unit tests for the agent harness registry (no Qt imports).

`spawn_argv` semantics mirror orchestrator.nvim's terminal.lua backends
table — these tests pin the per-harness flag/env contract the
QMLTermSession launch depends on.
"""

from __future__ import annotations

import json

from symmetria_ide.agent_harness import (
    HARNESSES,
    parse_opencode_sessions,
    spawn_argv,
)


# ---------------------------------------------------------------------------
# spawn_argv
# ---------------------------------------------------------------------------


def test_claude_fresh_dangerous_uses_flag_not_env():
    argv = spawn_argv(HARNESSES["claude"], "fresh", True, "42_1")
    assert argv == [
        "env",
        "SYMMETRIA_AGENT_ID=42_1",
        "claude",
        "--dangerously-skip-permissions",
    ]


def test_opencode_fresh_dangerous_uses_permission_env():
    argv = spawn_argv(HARNESSES["opencode"], "fresh", True, "42_1")
    assert argv == [
        "env",
        "SYMMETRIA_AGENT_ID=42_1",
        'OPENCODE_PERMISSION={"*":{"*":"allow"}}',
        "opencode",
    ]
    # The nested allow-all form is load-bearing (last-match-wins
    # permission resolution) — pin it against "simplification".
    env_pair = argv[2].split("=", 1)
    assert json.loads(env_pair[1]) == {"*": {"*": "allow"}}


def test_opencode_safe_variant_omits_permission_env():
    argv = spawn_argv(HARNESSES["opencode"], "fresh", False, "42_1")
    assert argv == ["env", "SYMMETRIA_AGENT_ID=42_1", "opencode"]


def test_opencode_continue_flag():
    argv = spawn_argv(HARNESSES["opencode"], "continue", False, "42_1")
    assert argv[-1] == "-c"


def test_opencode_resume_appends_session_id():
    argv = spawn_argv(HARNESSES["opencode"], "resume", False, "42_1", "ses_abc")
    assert argv[-2:] == ["--session", "ses_abc"]


def test_claude_resume_does_not_append_session_id():
    # claude's bare `-r` opens its own picker — a session id passed in
    # (e.g. by a confused caller) must not leak into the argv.
    argv = spawn_argv(HARNESSES["claude"], "resume", False, "42_1", "ses_abc")
    assert argv[-1] == "-r"
    assert "ses_abc" not in argv


# ---------------------------------------------------------------------------
# parse_opencode_sessions
# ---------------------------------------------------------------------------


def test_parse_sorts_newest_first_and_falls_back_title_to_id():
    stdout = json.dumps(
        [
            {"id": "ses_old", "title": "older", "updated": 1_000_000},
            {"id": "ses_new", "title": "", "updated": 2_000_000},
            {"id": "ses_created_only", "created": 1_500_000},
        ]
    )
    rows = parse_opencode_sessions(stdout)
    assert rows is not None
    assert [r["id"] for r in rows] == ["ses_new", "ses_created_only", "ses_old"]
    assert rows[0]["title"] == "ses_new"  # empty title falls back to id
    assert rows[2]["title"] == "older"
    assert all(r["when"] for r in rows)  # every row got a timestamp label


def test_parse_skips_non_dict_array_elements():
    # Non-dicts must be filtered BEFORE sorting — the sort key runs on
    # every element, so `[1, 2, {...}]` used to raise AttributeError.
    rows = parse_opencode_sessions('[1, "x", {"id": "ses_1"}]')
    assert rows is not None
    assert [r["id"] for r in rows] == ["ses_1"]


def test_parse_skips_entries_without_id():
    rows = parse_opencode_sessions('[{"title": "no id"}, {"id": "ses_1"}]')
    assert rows is not None
    assert [r["id"] for r in rows] == ["ses_1"]


def test_parse_empty_list_is_distinct_from_failure():
    assert parse_opencode_sessions("[]") == []


def test_parse_invalid_json_returns_none():
    assert parse_opencode_sessions("garbage") is None


def test_parse_non_list_json_returns_none():
    assert parse_opencode_sessions('{"not": "a list"}') is None
