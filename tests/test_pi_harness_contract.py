"""Cross-repository contract checks for the Pi harness integration.

The IDE phase freezes this document before the pi-agent reporter/MCP phases
implement it. These are intentionally structural: they require every load-
bearing wire value to be stated, while leaving prose and section layout free.

⚠ Wire values are DERIVED from the product code — `HARNESSES["pi"]`, the argv
`spawn_argv` actually builds, `_AGENT_WRITE_TOOLS` — never re-typed as test
literals. The contract's whole purpose is that the two repositories agree, so a
test that restated the values would keep passing while the doc drifted away
from the registry it describes. Renaming a registry field's value must fail
here until `docs/pi-harness-contract.md` is updated to match.
"""

import inspect
import re
from pathlib import Path

import pytest

from symmetria_ide.agent_harness import HARNESSES, spawn_argv
from symmetria_ide.app import _AGENT_WRITE_TOOLS
from symmetria_ide.browser_mcp import BrowserMcpServer

PI = HARNESSES["pi"]


def _contract_text() -> str:
    path = Path(__file__).resolve().parent.parent / "docs" / "pi-harness-contract.md"
    assert path.is_file(), "Phase 1 must freeze docs/pi-harness-contract.md"
    return path.read_text(encoding="utf-8")


def _claude_md_text() -> str:
    path = Path(__file__).resolve().parent.parent / "CLAUDE.md"
    assert path.is_file()
    return path.read_text(encoding="utf-8")


def _pi_spawn_argv() -> list[str]:
    """A maximal Pi spawn — every optional delivery channel populated, so the
    env keys the contract documents are read off a real argv."""
    return spawn_argv(
        PI,
        "fresh",
        True,
        "42_1",
        mcp_config_path="/tmp/agent-mcp.json",
        agent_sock_path="/run/user/1000/symmetria-ide-agents.sock",
        model="some-model",
        effort="high",
        extension_paths=("/tmp/symmetria-ide.ts",),
    )


def _env_keys(argv: list[str]) -> set[str]:
    """Names of the `KEY=value` pairs in the argv's `env …` wrapper."""
    return {
        tok.split("=", 1)[0] for tok in argv if "=" in tok and not tok.startswith("-")
    }


def _documents_flag(contract_text: str, flag: str) -> bool:
    """Is `flag` stated as a code span (``` `--x` ``` or ``` `--x <v>` ```)?

    Backtick-anchored so a two-character flag like `-c` cannot be satisfied by
    an accidental substring of the prose.
    """
    return re.search(rf"`{re.escape(flag)}[ `]", contract_text) is not None


# ---------------------------------------------------------------------------
# §1 — process identity and delivery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["SYMMETRIA_AGENT_ID", "SYMMETRIA_IDE_AGENT_SOCK"])
def test_contract_documents_env_the_spawn_actually_exports(name):
    """Both directions: the IDE really exports it, and the doc really says so.

    These two are built by the spawn path rather than named in the registry, so
    the argv is what pins them — a rename there breaks this test rather than
    silently stranding the pi-agent extension on a variable nobody sets.
    """
    assert name in _env_keys(_pi_spawn_argv())
    assert name in _contract_text()


def test_contract_documents_the_registry_delivery_env():
    contract_text = _contract_text()
    assert PI.mcp_config_path_env
    assert PI.mcp_config_path_env in _env_keys(_pi_spawn_argv())
    assert PI.mcp_config_path_env in contract_text
    # The extension override is read from the IDE's own environment (app.py),
    # so it is a registry name only — not an argv assignment.
    assert PI.extension_path_env
    assert PI.extension_path_env in contract_text


def test_spawn_env_table_is_exhaustive_or_explicitly_scoped():
    """The table must not look exhaustive while silently omitting exports.

    Pi currently inherits two Claude-oriented capability variables that its
    extension ignores. The contract may list them, or may explicitly say the
    table is limited to variables consumed by the Pi extension.
    """
    section = _contract_text().split("## 1.", 1)[1].split("## 2.", 1)[0]
    exported = _env_keys(_pi_spawn_argv())
    omitted = sorted(name for name in exported if name not in section)
    explicitly_scoped = "only variables consumed by the pi extension" in section.lower()
    assert omitted == [] or explicitly_scoped, (
        f"spawn exports undocumented variables {omitted}; list them or state "
        "that the table covers only variables consumed by the Pi extension"
    )


@pytest.mark.parametrize(
    "field",
    [
        "dangerous_flag",
        "resume_id_flag",
        "model_flag",
        "effort_flag",
        "extension_flag",
    ],
)
def test_contract_documents_every_registered_pi_flag(field):
    flag = getattr(PI, field)
    assert flag, f"pi registry lost its {field}"
    assert _documents_flag(_contract_text(), flag), (
        f"{field}={flag!r} is not stated in the contract doc"
    )


@pytest.mark.parametrize("spawn_type", ["continue", "resume"])
def test_contract_documents_the_spawn_type_flags(spawn_type):
    contract_text = _contract_text()
    flags = PI.flags[spawn_type]
    assert flags, f"pi lost its {spawn_type} flags"
    for flag in flags:
        assert _documents_flag(contract_text, flag)


def test_contract_documents_every_valid_thinking_level():
    """The `--thinking` row must enumerate exactly the registry's level set —
    a level the doc omits is one the pi side will reject as unknown."""
    (line,) = [line for line in _contract_text().splitlines() if "`--thinking " in line]
    stated = set(re.findall(r"[a-z]+", line.split("`--thinking ", 1)[1]))
    assert stated >= PI.valid_efforts
    # And nothing invented on the doc side either.
    assert stated - PI.valid_efforts == set()


def test_contract_records_the_approve_semantics_gap():
    """`--approve` is project trust, not skip-permissions. The doc must keep
    saying so — this is the one place Pi is honestly NOT at claude parity."""
    contract_text = _contract_text()
    assert "projectTrustOverride" in contract_text
    assert PI.judgeable_transcript is False


# ---------------------------------------------------------------------------
# §2 — the hook socket envelope
# ---------------------------------------------------------------------------


def test_contract_pins_hook_socket_envelope():
    contract_text = _contract_text()
    for field in (
        '"type"',
        '"hook"',
        '"agent_id"',
        '"hook_event_name"',
        '"session_id"',
        '"cwd"',
        '"event_ts_ns"',
        '"tool_name"',
        '"tool_path"',
    ):
        assert field in contract_text


@pytest.mark.parametrize(
    ("pi_name", "ide_name"),
    [
        ("bash", "Bash"),
        ("edit", "Edit"),
        ("write", "Write"),
        ("read", "Read"),
        ("grep", "Grep"),
        ("find", "Glob"),
        ("ls", "Read"),
    ],
)
def test_contract_pins_tool_name_normalisation(pi_name, ide_name):
    contract_text = _contract_text()
    assert any(
        pi_name in line and ide_name in line for line in contract_text.splitlines()
    ), f"missing explicit {pi_name!r} -> {ide_name!r} mapping"


@pytest.mark.parametrize(
    ("ide_name", "is_write"),
    [
        ("Edit", True),
        ("Write", True),
        ("Read", False),
        ("Grep", False),
        ("Glob", False),
    ],
)
def test_normalisation_targets_land_on_the_intended_side_of_write_attribution(
    ide_name, is_write
):
    """The normalisation table exists so `_AGENT_WRITE_TOOLS` needs no Pi entry.

    Asserted against the real set: if a future edit moves `Read`/`Glob` into it,
    Pi's `read`/`find`/`ls` would start yanking the chrome on exploration, and
    the contract's stated rationale would be silently false.
    """
    assert (ide_name in _AGENT_WRITE_TOOLS) is is_write


# ---------------------------------------------------------------------------
# §3 — the MCP config file
# ---------------------------------------------------------------------------


def test_contract_pins_mcp_file_schema_and_attribution():
    contract_text = _contract_text()
    for token in (
        "mcpServers",
        "symmetria-browser",
        "chrome-devtools",
        "X-Symmetria-Agent",
        "http",
        "stdio",
    ):
        assert token in contract_text


def test_contract_pins_root_session_only_reporting():
    contract_text = _contract_text()
    assert "subagent-sessions" in contract_text
    assert "root" in contract_text.lower()


def test_contract_routes_nested_pi_events_by_session_not_inherited_env_id():
    """Nested Pi processes inherit the parent's slot id and MCP config.

    Root/subagent guards do not catch a separate Pi launched from a tool shell.
    The frozen contract therefore makes registry resolution from the reporting
    session/cwd authoritative and names the inheritance hazard explicitly.
    """
    contract_text = _contract_text()
    identity = contract_text.split("## 1.", 1)[1].split("## 2.", 1)[0]
    root_guard = contract_text.split("### 2.3", 1)[1].split("## 3.", 1)[0]
    combined = f"{identity}\n{root_guard}".lower()

    assert "does not re-resolve" not in identity.lower()
    assert "inherited" in combined and "nested" in combined
    assert "symmetria_ide_mcp_config" in combined
    assert "resolve_slot_for_event" in root_guard
    assert "session_id" in root_guard and "cwd" in root_guard
    assert "authoritative" in combined


def test_agent_id_envelope_field_is_documented_as_an_untrusted_hint():
    field_line = next(
        line
        for line in _contract_text().splitlines()
        if line.startswith('| `"agent_id"`')
    ).lower()
    assert "not authoritative" in field_line or "untrusted" in field_line


def test_agent_config_path_docstring_names_both_file_delivery_forms():
    doc = inspect.getdoc(BrowserMcpServer.agent_config_path) or ""
    assert "--mcp-config" in doc
    assert PI.mcp_config_path_env in doc
    assert "wants_mcp_config_file" in doc


def test_always_loaded_chord_docs_follow_harness_metadata():
    """CLAUDE.md is loaded into every work session, so stale routing claims
    are operational regressions rather than harmless prose drift."""
    text = _claude_md_text()
    chords = text.split("**Chords.**", 1)[1].split("## The agentic browser", 1)[0]
    lowered = chords.lower()

    assert "agentharness.scroll_page_fraction" in lowered
    assert "claude/opencode" in lowered
    assert "pi" in lowered and "0.5" in lowered
    assert "predicted" in lowered and "measure" in lowered

    alt_m = chords.split("`Alt+M`", 1)[1].split("`Ctrl+M`", 1)[0]
    assert "model_slash_command" in alt_m
    assert "Claude" in alt_m and "Pi" in alt_m
    assert "OpenCode" in alt_m and "no-op" in alt_m


# ---------------------------------------------------------------------------
# §4 — stated non-coverage
# ---------------------------------------------------------------------------


def test_contract_states_the_reporter_dependency_of_coordination():
    """Until the reporter phase ships, Pi emits no busy→idle edge at all, so a
    `wait_for_agent` registration on a Pi agent arms and never fires. Recording
    that phase boundary here stops a later phase from debugging it as a
    coordination fault. Delete this test when the reporter lands."""
    contract_text = _contract_text()
    section = contract_text.split("## 4.", 1)[1]
    assert "wait_for_agent" in section
    assert "arm" in section
