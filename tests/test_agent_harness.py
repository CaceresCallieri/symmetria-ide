"""Pure unit tests for the agent harness registry (no Qt imports).

`spawn_argv` semantics mirror orchestrator.nvim's terminal.lua backends
table — these tests pin the per-harness flag/env contract the
QMLTermSession launch depends on.
"""

from __future__ import annotations

import json

from symmetria_ide.agent_harness import (
    CHILD_SESSION_UNSET_ARGS,
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
        *CHILD_SESSION_UNSET_ARGS,
        "SYMMETRIA_AGENT_ID=42_1",
        "claude",
        "--dangerously-skip-permissions",
    ]


def test_opencode_fresh_dangerous_uses_permission_env():
    argv = spawn_argv(HARNESSES["opencode"], "fresh", True, "42_1")
    assert argv == [
        "env",
        *CHILD_SESSION_UNSET_ARGS,
        "SYMMETRIA_AGENT_ID=42_1",
        'OPENCODE_PERMISSION={"*":{"*":"allow"}}',
        "opencode",
    ]
    # The nested allow-all form is load-bearing (last-match-wins
    # permission resolution) — pin it against "simplification".
    env_pair = next(a for a in argv if a.startswith("OPENCODE_PERMISSION=")).split(
        "=", 1
    )
    assert json.loads(env_pair[1]) == {"*": {"*": "allow"}}


def test_opencode_safe_variant_omits_permission_env():
    argv = spawn_argv(HARNESSES["opencode"], "fresh", False, "42_1")
    assert argv == [
        "env",
        *CHILD_SESSION_UNSET_ARGS,
        "SYMMETRIA_AGENT_ID=42_1",
        "opencode",
    ]


def test_opencode_continue_flag():
    argv = spawn_argv(HARNESSES["opencode"], "continue", False, "42_1")
    assert argv[-1] == "-c"


def test_opencode_resume_appends_session_id():
    argv = spawn_argv(HARNESSES["opencode"], "resume", False, "42_1", "ses_abc")
    assert argv[-2:] == ["--session", "ses_abc"]


def test_claude_resume_with_id_appends_for_noninteractive_resume():
    # `-r <id>` resumes that exact session non-interactively — what session
    # restore replays to re-home a claude conversation without a manual pick.
    argv = spawn_argv(HARNESSES["claude"], "resume", False, "42_1", "ses_abc")
    assert argv[-2:] == ["-r", "ses_abc"]


def test_claude_resume_without_id_keeps_bare_picker_flag():
    # No id → bare `-r`, which opens claude's own interactive picker.
    argv = spawn_argv(HARNESSES["claude"], "resume", False, "42_1")
    assert argv[-1] == "-r"
    assert "ses_abc" not in argv


# ---------------------------------------------------------------------------
# IDE activity-reporter injection (--settings + SYMMETRIA_IDE_AGENT_SOCK)
# ---------------------------------------------------------------------------


def test_claude_injects_settings_and_sock_env():
    settings = '{"hooks":{"Stop":[]}}'
    argv = spawn_argv(
        HARNESSES["claude"],
        "fresh",
        True,
        "42_1",
        settings_json=settings,
        agent_sock_path="/run/user/1000/symmetria-ide-agents-42.sock",
    )
    # The sock env rides the env wrapper, right after the agent id, followed by
    # the status-line capability advert.
    assert argv[: 4 + len(CHILD_SESSION_UNSET_ARGS)] == [
        "env",
        *CHILD_SESSION_UNSET_ARGS,
        "SYMMETRIA_AGENT_ID=42_1",
        "SYMMETRIA_IDE_AGENT_SOCK=/run/user/1000/symmetria-ide-agents-42.sock",
        "SYMMETRIA_IDE_STATUSLINE_TAP=1",
    ]
    # The settings string is passed inline after the --settings flag.
    assert "--settings" in argv
    assert argv[argv.index("--settings") + 1] == settings


def test_claude_settings_is_single_inline_arg():
    # The whole JSON must be ONE argv element (inline string form), not split.
    settings = '{"hooks":{"PreToolUse":[{"hooks":[{"type":"command"}]}]}}'
    argv = spawn_argv(
        HARNESSES["claude"], "fresh", False, "42_1", settings_json=settings
    )
    assert argv.count("--settings") == 1
    assert argv[argv.index("--settings") + 1] == settings


def test_opencode_ignores_settings_but_keeps_sock_env():
    # opencode has no settings_flag → no --settings; the sock env is harmless
    # (no reporter reads it) but exported uniformly.
    argv = spawn_argv(
        HARNESSES["opencode"],
        "fresh",
        False,
        "42_1",
        settings_json='{"hooks":{}}',
        agent_sock_path="/run/user/1000/symmetria-ide-agents-42.sock",
    )
    assert "--settings" not in argv
    assert (
        "SYMMETRIA_IDE_AGENT_SOCK=/run/user/1000/symmetria-ide-agents-42.sock" in argv
    )


def test_no_settings_when_json_empty():
    argv = spawn_argv(HARNESSES["claude"], "fresh", True, "42_1")
    assert "--settings" not in argv
    assert not any(a.startswith("SYMMETRIA_IDE_AGENT_SOCK=") for a in argv)
    # The status-line advert rides the sock — absent when there's no sock.
    assert "SYMMETRIA_IDE_STATUSLINE_TAP=1" not in argv


def test_statusline_tap_advert_rides_the_sock():
    argv = spawn_argv(
        HARNESSES["claude"],
        "fresh",
        True,
        "42_1",
        agent_sock_path="/run/user/1000/symmetria-ide-agents-42.sock",
    )
    assert "SYMMETRIA_IDE_STATUSLINE_TAP=1" in argv


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


# ---------------------------------------------------------------------------
# spawn_argv — Stage 2c browser MCP --mcp-config injection
# ---------------------------------------------------------------------------


def test_claude_injects_mcp_config_when_path_given():
    argv = spawn_argv(
        HARNESSES["claude"], "fresh", True, "42_1", mcp_config_path="/tmp/cfg.json"
    )
    assert "--mcp-config" in argv
    assert argv[argv.index("--mcp-config") + 1] == "/tmp/cfg.json"
    # Injected after the executable (it's a claude CLI flag, not an env pair).
    assert argv.index("--mcp-config") > argv.index("claude")


def test_no_mcp_config_when_path_empty():
    argv = spawn_argv(HARNESSES["claude"], "fresh", True, "42_1")
    assert "--mcp-config" not in argv


def test_opencode_ignores_mcp_config_path():
    # opencode has no --mcp-config flag (mcp_config_flag is None) — the FILE-PATH
    # form is claude-only. opencode injects via mcp_config_content instead (its
    # remote MCP is SSE-only and configured through OPENCODE_CONFIG_CONTENT).
    argv = spawn_argv(
        HARNESSES["opencode"], "fresh", True, "42_1", mcp_config_path="/tmp/cfg.json"
    )
    assert "--mcp-config" not in argv
    assert "/tmp/cfg.json" not in argv


def test_mcp_config_coexists_with_resume_session_id():
    argv = spawn_argv(
        HARNESSES["opencode"],
        "resume",
        True,
        "42_1",
        session_id="ses_abc",
        mcp_config_path="/tmp/cfg.json",
    )
    # opencode drops the mcp flag but still appends the session id last.
    assert argv[-1] == "ses_abc"


# ---------------------------------------------------------------------------
# spawn_argv — opencode inline MCP config (OPENCODE_CONFIG_CONTENT env var)
# ---------------------------------------------------------------------------


def test_opencode_injects_mcp_config_content_env():
    # opencode's browser MCP rides an env var, NOT --mcp-config: the inline JSON
    # is exported as OPENCODE_CONFIG_CONTENT in the env wrapper (before the
    # executable), never as a flag.
    content = '{"mcp":{"symmetria-browser":{"type":"remote"}}}'
    argv = spawn_argv(
        HARNESSES["opencode"], "fresh", False, "42_1", mcp_config_content=content
    )
    assert f"OPENCODE_CONFIG_CONTENT={content}" in argv
    # It's an env pair → before the executable, and NOT a --mcp-config flag.
    assert argv.index(f"OPENCODE_CONFIG_CONTENT={content}") < argv.index("opencode")
    assert "--mcp-config" not in argv


def test_claude_ignores_mcp_config_content():
    # claude has no mcp_config_env (it uses the --mcp-config FILE form) → the
    # inline content is silently dropped.
    argv = spawn_argv(
        HARNESSES["claude"], "fresh", False, "42_1", mcp_config_content='{"mcp":{}}'
    )
    assert not any(a.startswith("OPENCODE_CONFIG_CONTENT=") for a in argv)


def test_no_mcp_config_content_when_empty():
    argv = spawn_argv(HARNESSES["opencode"], "fresh", False, "42_1")
    assert not any(a.startswith("OPENCODE_CONFIG_CONTENT=") for a in argv)


def test_opencode_mcp_content_coexists_with_dangerous_permission_env():
    # Two INDEPENDENT env vars: dangerous mode (OPENCODE_PERMISSION) and the
    # browser MCP (OPENCODE_CONFIG_CONTENT) both ride the env wrapper, both
    # precede the executable.
    content = '{"mcp":{}}'
    argv = spawn_argv(
        HARNESSES["opencode"], "fresh", True, "42_1", mcp_config_content=content
    )
    assert any(a.startswith("OPENCODE_PERMISSION=") for a in argv)
    exe = argv.index("opencode")
    assert argv.index(f"OPENCODE_CONFIG_CONTENT={content}") < exe


# ---------------------------------------------------------------------------
# spawn_argv — per-project model / effort launch defaults
# ---------------------------------------------------------------------------


def test_claude_appends_model_and_effort_flags():
    argv = spawn_argv(
        HARNESSES["claude"], "fresh", False, "42_1", model="opus", effort="high"
    )
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "high"
    # Both are claude CLI flags, so they sit after the executable.
    assert argv.index("--model") > argv.index("claude")
    assert argv.index("--effort") > argv.index("claude")


def test_no_model_or_effort_flags_when_empty():
    argv = spawn_argv(HARNESSES["claude"], "fresh", False, "42_1")
    assert "--model" not in argv
    assert "--effort" not in argv


def test_claude_model_without_effort():
    argv = spawn_argv(HARNESSES["claude"], "fresh", False, "42_1", model="sonnet")
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "--effort" not in argv


def test_claude_effort_without_model():
    argv = spawn_argv(HARNESSES["claude"], "fresh", False, "42_1", effort="xhigh")
    assert argv[argv.index("--effort") + 1] == "xhigh"
    assert "--model" not in argv


def test_claude_invalid_effort_is_dropped():
    # A committed typo ("ultra" is not a level) must NOT reach --effort, or the
    # launch would fail. Model still passes (it isn't validated here).
    argv = spawn_argv(
        HARNESSES["claude"], "fresh", False, "42_1", model="opus", effort="ultra"
    )
    assert "--effort" not in argv
    assert "ultra" not in argv
    assert argv[argv.index("--model") + 1] == "opus"


def test_claude_all_valid_effort_levels_pass():
    for level in ("low", "medium", "high", "xhigh", "max"):
        argv = spawn_argv(HARNESSES["claude"], "fresh", False, "42_1", effort=level)
        assert argv[argv.index("--effort") + 1] == level


def test_opencode_uses_model_and_variant_flags():
    # opencode maps model→--model, effort→--variant; valid_efforts is empty so
    # any provider-specific variant string passes unvalidated.
    argv = spawn_argv(
        HARNESSES["opencode"],
        "fresh",
        False,
        "42_1",
        model="anthropic/claude-opus-4-8",
        effort="high",
    )
    assert argv[argv.index("--model") + 1] == "anthropic/claude-opus-4-8"
    assert argv[argv.index("--variant") + 1] == "high"
    assert "--effort" not in argv  # opencode's effort flag is --variant


def test_opencode_unvalidated_effort_passes_through():
    # No valid_efforts set → even an arbitrary string reaches --variant.
    argv = spawn_argv(
        HARNESSES["opencode"], "fresh", False, "42_1", effort="whatever-provider-uses"
    )
    assert argv[argv.index("--variant") + 1] == "whatever-provider-uses"


def test_model_effort_coexist_with_resume_session_id():
    # The session id must stay LAST even with model/effort flags present.
    argv = spawn_argv(
        HARNESSES["claude"],
        "resume",
        False,
        "42_1",
        session_id="ses_abc",
        model="opus",
        effort="high",
    )
    assert argv[-1] == "ses_abc"
    assert "--model" in argv and "--effort" in argv
