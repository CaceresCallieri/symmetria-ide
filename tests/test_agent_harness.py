"""Pure unit tests for the agent harness registry (no Qt imports).

`spawn_argv` semantics mirror orchestrator.nvim's terminal.lua backends
table — these tests pin the per-harness flag/env contract the
QMLTermSession launch depends on.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from symmetria_ide.agent_harness import (
    CLAUDE_ENV_UNSET_ARGS,
    HARNESSES,
    MENU_ORDERED_HARNESSES,
    parse_opencode_sessions,
    spawn_argv,
    tmux_session_name,
    tmux_wrap,
)

QML_DIR = Path(__file__).resolve().parents[1] / "qml"


def test_harness_menu_metadata_is_complete_and_ordered():
    """The spawn chooser is projected from the registry, not a QML side table."""
    expected = {
        "pi": {
            "menu_key": "P",
            "icon": "assets/pi-icon.svg",
            "resume_label": "resume (pi's picker)",
            "menu_order": 0,
        },
        "claude": {
            "menu_key": "C",
            "icon": "assets/claude-icon.svg",
            "resume_label": "resume (claude's picker)",
            "menu_order": 1,
        },
        "opencode": {
            "menu_key": "O",
            "icon": "assets/opencode-icon.svg",
            "resume_label": "resume (session picker)",
            "menu_order": 2,
        },
    }

    assert set(HARNESSES) == set(expected)
    for name, values in expected.items():
        harness = HARNESSES[name]
        assert {
            "menu_key": harness.menu_key,
            "icon": harness.icon,
            "resume_label": harness.resume_label,
            "menu_order": harness.menu_order,
        } == values

    assert [harness.name for harness in MENU_ORDERED_HARNESSES] == [
        "pi",
        "claude",
        "opencode",
    ]


def test_menu_keys_are_unique_single_uppercase_letters():
    """The chooser dispatches on `menuKey.charCodeAt(0) === event.key`.

    Qt's letter key enum IS the uppercase ASCII code, so a lowercase or
    multi-character key would simply never match any keypress — a row that
    renders correctly and cannot be selected. A duplicate key would make the
    earlier registry entry win and the later one equally unreachable.
    """
    keys = [harness.menu_key for harness in HARNESSES.values()]
    for key in keys:
        assert len(key) == 1, f"{key!r} must be a single character"
        assert key.isalpha() and key.isupper(), f"{key!r} must be an uppercase letter"
    assert len(set(keys)) == len(keys), f"duplicate menu_key in {keys}"


def test_menu_orders_are_unique():
    """A tie makes the chooser's row order depend on dict insertion order."""
    orders = [harness.menu_order for harness in HARNESSES.values()]
    assert len(set(orders)) == len(orders), f"duplicate menu_order in {orders}"


def test_every_registered_icon_exists_under_qml():
    """`icon` is resolved against qml/ at runtime; a typo is a blank row.

    Qt's Image fails a missing source with a log line the IDE's message
    handler swallows, so the only symptom is a harness row with no brand mark.
    """
    for harness in HARNESSES.values():
        icon = QML_DIR / harness.icon
        assert icon.is_file(), f"{harness.name} icon missing: {icon}"


# ---------------------------------------------------------------------------
# spawn_argv
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("harness", "spawn_type", "session_id", "tail"),
    [
        ("claude", "fresh", "", []),
        ("claude", "fresh", "ses_abc", []),
        ("claude", "continue", "", ["-c"]),
        ("claude", "continue", "ses_abc", ["-c"]),
        ("claude", "resume", "", ["-r"]),
        ("claude", "resume", "ses_abc", ["-r", "ses_abc"]),
        ("opencode", "fresh", "", []),
        ("opencode", "fresh", "ses_abc", []),
        ("opencode", "continue", "", ["-c"]),
        ("opencode", "continue", "ses_abc", ["-c"]),
        ("opencode", "resume", "", ["--session"]),
        ("opencode", "resume", "ses_abc", ["--session", "ses_abc"]),
    ],
)
def test_existing_harness_argv_stays_byte_identical_across_resume_refactor(
    harness, spawn_type, session_id, tail
):
    """Adding resume_id_flag must not perturb either existing harness.

    This intentionally pins all six spawn_type/session-id permutations for
    each harness, including the controller-rejected bare OpenCode resume. It
    is a before-Pi snapshot, not an expectation derived from registry fields.
    """
    executable = HARNESSES[harness].executable
    expected = [
        "env",
        *CLAUDE_ENV_UNSET_ARGS,
        "SYMMETRIA_AGENT_ID=42_1",
        executable,
        *(HARNESSES[harness].base_flags),
        *tail,
    ]
    assert (
        spawn_argv(
            HARNESSES[harness],
            spawn_type,
            False,
            "42_1",
            session_id=session_id,
        )
        == expected
    )


def test_pi_registry_contract():
    pi = HARNESSES["pi"]
    assert pi.name == "pi"
    assert pi.executable == "pi"  # production resolves the installed binary
    assert pi.label == "Pi"
    assert pi.flags == {"fresh": [], "continue": ["-c"], "resume": ["-r"]}
    assert pi.resume_id_flag == "--session"
    assert pi.resume_requires_id is False
    assert pi.dangerous_flag == "--approve"
    assert pi.mcp_config_path_env == "SYMMETRIA_IDE_MCP_CONFIG"
    assert pi.extension_flag == "--extension"
    assert pi.model_flag == "--model"
    assert pi.effort_flag == "--thinking"
    assert pi.valid_efforts == frozenset(
        {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
    )
    assert pi.scroll_page_fraction == 0.5
    assert pi.model_slash_command == "/model"
    assert "pi" in pi.title_placeholders


# ---------------------------------------------------------------------------
# Registry invariants — resume-by-id has exactly one legal shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(HARNESSES))
def test_every_registered_harness_declares_a_resume_id_flag(name):
    """A registered harness must name the flag its resume-by-id uses.

    `spawn_argv` refuses to fall back to "picker flags + trailing id" because
    that shape is silently read as a chat message by pi. Any harness the IDE
    can hand a persisted session id to (all of them — restore_session is
    harness-agnostic) must therefore declare the flag explicitly.
    """
    harness = HARNESSES[name]
    assert "resume" in harness.flags
    assert harness.resume_id_flag, (
        f"{name} can be handed a restored session id but names no resume_id_flag"
    )
    picker_flags = harness.flags["resume"]
    assert len(picker_flags) <= 1, (
        f"{name} declares extra resume picker tokens {picker_flags!r}; "
        "spawn_argv replaces the whole picker form when restoring by id, so "
        "any token still required for exact restore would be silently lost"
    )


def test_resume_by_id_without_a_flag_raises_instead_of_corrupting_argv():
    """Malformed harness metadata must fail loudly, not build `-r <uuid>`."""
    broken = replace(HARNESSES["pi"], name="broken", resume_id_flag=None)
    with pytest.raises(ValueError) as excinfo:
        spawn_argv(broken, "resume", False, "42_1", session_id="ses_abc")
    message = str(excinfo.value)
    assert "resume_id_flag" in message
    assert "broken" in message
    # The picker path is unaffected — only the id form is refused.
    assert spawn_argv(broken, "resume", False, "42_1")[-1] == "-r"


@pytest.mark.parametrize(
    ("name", "judgeable"),
    [("claude", True), ("opencode", False), ("pi", False)],
)
def test_judgeable_transcript_is_capability_metadata(name, judgeable):
    """Only claude's transcript is readable by the coordination judge today."""
    assert HARNESSES[name].judgeable_transcript is judgeable


@pytest.mark.parametrize(
    ("spawn_type", "session_id", "tail"),
    [
        ("fresh", "", []),
        ("continue", "", ["-c"]),
        ("resume", "", ["-r"]),
        ("resume", "019fc9e5-dead-beef", ["--session", "019fc9e5-dead-beef"]),
    ],
)
def test_pi_spawn_and_resume_forms(spawn_type, session_id, tail):
    argv = spawn_argv(
        HARNESSES["pi"],
        spawn_type,
        False,
        "42_1",
        session_id=session_id,
    )
    assert argv == [
        "env",
        *CLAUDE_ENV_UNSET_ARGS,
        "SYMMETRIA_AGENT_ID=42_1",
        "pi",
        *tail,
    ]
    if session_id:
        assert ["-r", session_id] != argv[-2:]


def test_pi_permissive_variant_adds_approve_and_safe_variant_omits_it():
    permissive = spawn_argv(HARNESSES["pi"], "fresh", True, "42_1")
    safe = spawn_argv(HARNESSES["pi"], "fresh", False, "42_1")
    assert permissive[-2:] == ["pi", "--approve"]
    assert "--approve" not in safe


@pytest.mark.parametrize(
    "level", ["off", "minimal", "low", "medium", "high", "xhigh", "max"]
)
def test_pi_accepts_every_documented_thinking_level(level):
    argv = spawn_argv(HARNESSES["pi"], "fresh", False, "42_1", effort=level)
    assert argv[argv.index("--thinking") + 1] == level


def test_pi_model_and_invalid_thinking_value():
    argv = spawn_argv(
        HARNESSES["pi"],
        "fresh",
        False,
        "42_1",
        model="anthropic/claude-sonnet-4-5",
        effort="ultra",
    )
    assert argv[argv.index("--model") + 1] == "anthropic/claude-sonnet-4-5"
    assert "--thinking" not in argv
    assert "ultra" not in argv


def test_pi_receives_mcp_config_path_via_env_not_cli_flag():
    argv = spawn_argv(
        HARNESSES["pi"],
        "fresh",
        False,
        "42_1",
        mcp_config_path="/tmp/symmetria-agent-42_1.json",
    )
    config_env = "SYMMETRIA_IDE_MCP_CONFIG=/tmp/symmetria-agent-42_1.json"
    assert config_env in argv
    assert argv.index(config_env) < argv.index("pi")
    assert "--mcp-config" not in argv


def test_pi_extension_paths_use_declared_extension_flag():
    argv = spawn_argv(
        HARNESSES["pi"],
        "fresh",
        False,
        "42_1",
        extension_paths=("/worktrees/pi-agent/extensions/symmetria-ide.ts",),
    )
    assert argv[-2:] == [
        "--extension",
        "/worktrees/pi-agent/extensions/symmetria-ide.ts",
    ]


def test_claude_fresh_dangerous_uses_flag_not_env():
    argv = spawn_argv(HARNESSES["claude"], "fresh", True, "42_1")
    assert argv == [
        "env",
        *CLAUDE_ENV_UNSET_ARGS,
        "SYMMETRIA_AGENT_ID=42_1",
        "claude",
        "--no-chrome",
        "--dangerously-skip-permissions",
    ]


def test_claude_always_disables_claude_in_chrome():
    # Containment: with the user's global claudeInChromeDefaultEnabled, a bare
    # claude would auto-connect the Claude-in-Chrome extension (the user's REAL
    # Chrome) — escaped windows, no chip globe, no attribution, and agents
    # picking it over the injected symmetria-browser tools. Every claude spawn
    # (safe or dangerous, any spawn type) must carry --no-chrome.
    for spawn_type in ("fresh", "continue", "resume"):
        for dangerous in (True, False):
            argv = spawn_argv(HARNESSES["claude"], spawn_type, dangerous, "42_1")
            assert "--no-chrome" in argv


def test_opencode_has_no_chrome_flag():
    # --no-chrome is a claude CLI flag; opencode would fail to launch with it.
    argv = spawn_argv(HARNESSES["opencode"], "fresh", True, "42_1")
    assert "--no-chrome" not in argv


def test_opencode_fresh_dangerous_uses_permission_env():
    argv = spawn_argv(HARNESSES["opencode"], "fresh", True, "42_1")
    assert argv == [
        "env",
        *CLAUDE_ENV_UNSET_ARGS,
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
        *CLAUDE_ENV_UNSET_ARGS,
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
    assert argv[: 5 + len(CLAUDE_ENV_UNSET_ARGS)] == [
        "env",
        *CLAUDE_ENV_UNSET_ARGS,
        "SYMMETRIA_AGENT_ID=42_1",
        "SYMMETRIA_IDE_AGENT_SOCK=/run/user/1000/symmetria-ide-agents-42.sock",
        "SYMMETRIA_IDE_STATUSLINE_TAP=1",
        "SYMMETRIA_IDE_USAGE_PANEL=1",
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


def test_usage_panel_advert_is_separate_from_the_tap_advert():
    # Two adverts, not one: `status-line.sh` omits its 5h/7d segment on the
    # USAGE_PANEL var alone. Collapsing them would blank usage in the stable
    # build, which sets the tap var but has no panel. Both ride the sock.
    with_sock = spawn_argv(
        HARNESSES["claude"],
        "fresh",
        True,
        "42_1",
        agent_sock_path="/run/user/1000/symmetria-ide-agents-42.sock",
    )
    assert "SYMMETRIA_IDE_USAGE_PANEL=1" in with_sock
    assert "SYMMETRIA_IDE_USAGE_PANEL=1" not in spawn_argv(
        HARNESSES["claude"], "fresh", True, "42_1"
    )


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


def test_model_effort_precede_continue_flag():
    # The deliberate ordering (model/effort BEFORE the spawn-type flag) must
    # hold for `continue` (-c) too, not just fresh/resume — a regression that
    # only reordered the continue path would otherwise slip through.
    argv = spawn_argv(
        HARNESSES["claude"], "continue", False, "42_1", model="opus", effort="high"
    )
    assert argv.index("--model") < argv.index("-c")
    assert argv.index("--effort") < argv.index("-c")


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


# ---------------------------------------------------------------------------
# executable override (tmux mode uses an absolute path)
# ---------------------------------------------------------------------------


def test_executable_override_replaces_bare_name():
    argv = spawn_argv(
        HARNESSES["claude"], "fresh", False, "42_1", executable="/usr/bin/claude"
    )
    assert "/usr/bin/claude" in argv
    assert "claude" not in argv  # the bare name is gone
    # It replaces the program element (ends the env prefix), so every following
    # arg is still the program's, not env's.
    assert argv[argv.index("/usr/bin/claude") - 1].startswith("SYMMETRIA_AGENT_ID=")


def test_executable_empty_keeps_bare_name():
    # The default ("") preserves the legacy bare-name behavior byte-for-byte.
    assert spawn_argv(HARNESSES["claude"], "fresh", False, "42_1") == spawn_argv(
        HARNESSES["claude"], "fresh", False, "42_1", executable=""
    )
    assert "claude" in spawn_argv(HARNESSES["claude"], "fresh", False, "42_1")


# ---------------------------------------------------------------------------
# tmux_session_name
# ---------------------------------------------------------------------------


def test_tmux_session_name_slug_hash_and_slot():
    # <slug>-<hash6>-<slot>: readable slug prefix, 6-hex path hash, slot suffix.
    name = tmux_session_name("/home/jc/projects/vigilia", 1)
    assert name.startswith("vigilia-") and name.endswith("-1")
    parts = name.split("-")
    assert parts[0] == "vigilia"
    assert len(parts[-2]) == 6 and all(c in "0123456789abcdef" for c in parts[-2])
    assert parts[-1] == "1"
    # Pin the WHOLE format contract in one place — in particular that no tmux-
    # forbidden char ('.'/':') can ever appear in the composed name.
    assert re.fullmatch(r"[a-z0-9]+-[0-9a-f]{6}-\d+", name)


def test_tmux_session_name_slugifies_forbidden_chars():
    # tmux forbids '.' and ':' in session names; the slug must reduce them.
    name = tmux_session_name("/home/jc/my.cool:proj", 3)
    assert name.startswith("my-cool-proj-") and name.endswith("-3")
    assert "." not in name and ":" not in name


def test_tmux_session_name_trailing_slash_and_case():
    name = tmux_session_name("/home/jc/Projects/Symmetria-IDE/", 2)
    assert name.startswith("symmetria-ide-") and name.endswith("-2")


def test_tmux_session_name_empty_falls_back_to_agent():
    assert tmux_session_name("", 4).startswith("agent-")
    assert tmux_session_name("", 4).endswith("-4")


def test_tmux_session_name_disambiguates_same_basename():
    # The whole point: two distinct roots sharing a basename must NOT collide on
    # the shared socket (else new-session -A attaches the wrong project's session).
    a = tmux_session_name("/home/jc/clients/acme/app", 1)
    b = tmux_session_name("/home/jc/clients/globex/app", 1)
    assert a != b
    assert a.startswith("app-") and b.startswith("app-")


def test_tmux_session_name_is_deterministic():
    # Same path → same name (no pid), so a restarted IDE re-adopts via -A.
    assert tmux_session_name("/home/jc/projects/vigilia", 2) == tmux_session_name(
        "/home/jc/projects/vigilia", 2
    )


# ---------------------------------------------------------------------------
# tmux_wrap
# ---------------------------------------------------------------------------


def test_tmux_wrap_attaches_or_creates_with_inner_last():
    inner = ["env", "SYMMETRIA_AGENT_ID=42_1", "claude"]
    argv = tmux_wrap(inner, "/run/user/1000/agent.sock", "vigilia-1")
    assert argv == [
        "tmux",
        "-S",
        "/run/user/1000/agent.sock",
        "new-session",
        "-A",  # attach-if-exists → a restarted IDE re-adopts, no double-spawn
        "-s",
        "vigilia-1",
        *inner,  # inner appended verbatim, LAST
    ]


def test_tmux_wrap_conf_adds_server_flag_before_command():
    argv = tmux_wrap(["env", "claude"], "/s.sock", "vigilia-1", conf_path="/c.conf")
    # -f is a SERVER flag: it must sit with -S, before `new-session`.
    assert argv[:5] == ["tmux", "-S", "/s.sock", "-f", "/c.conf"]
    assert argv[5] == "new-session"


def test_tmux_wrap_start_directory_adds_c_flag():
    argv = tmux_wrap(
        ["env", "claude"], "/s.sock", "vigilia-1", start_directory="/home/jc/projects/x"
    )
    assert argv[argv.index("-c") + 1] == "/home/jc/projects/x"
    # -c is a new-session flag: after `new-session`, before the inner argv.
    assert argv.index("new-session") < argv.index("-c") < argv.index("env")


def test_tmux_wrap_preserves_inline_settings_json():
    # The load-bearing property: an inline --settings JSON (spaces/braces/quotes)
    # rides through as ONE element and stays intact — tmux execs the inner argv
    # directly (no shell re-split). Pin it against a wrap that shells the command.
    inner = spawn_argv(
        HARNESSES["claude"],
        "fresh",
        True,
        "42_1",
        settings_json='{"hooks": {"Stop": [{"type": "command"}]}}',
    )
    argv = tmux_wrap(inner, "/s.sock", "vigilia-1")
    assert '{"hooks": {"Stop": [{"type": "command"}]}}' in argv
    # And the whole inner block is a contiguous suffix (nothing interleaved).
    assert argv[-len(inner) :] == inner
