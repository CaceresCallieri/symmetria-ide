"""Agent harness registry — which CLI an agent slot runs and how.

A "harness" is the agent CLI a terminal-agent slot hosts (claude,
opencode, …). The registry mirrors orchestrator.nvim's
`terminal.lua::M.backends` semantics (the authoritative prior art for
per-CLI spawn flags), renamed per the project's terminology choice.

Pure module: no Qt imports, fully unit-testable. AppController consumes
`spawn_argv` for the QMLTermSession launch and `parse_opencode_sessions`
for the resume picker's session list.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field


@dataclass(slots=True, frozen=True)
class AgentHarness:
    """Per-CLI spawn semantics for one agent harness.

    "Dangerous" (skip-permissions) is expressed differently per harness:
    a launch flag for claude (`dangerous_flag`), an env var for opencode
    (`dangerous_env`) — the bare opencode TUI accepts no permission flag.
    """

    name: str
    executable: str
    label: str
    # Flag appended when spawning dangerous (claude); None = no flag form.
    dangerous_flag: str | None = None
    # Env pairs exported when spawning dangerous (opencode). (k, v) tuples
    # because frozen dataclass fields must stay immutable.
    dangerous_env: tuple[tuple[str, str], ...] = ()
    # spawn_type -> CLI flags. `resume` flags get the session id appended
    # when `resume_requires_id` (opencode `--session <id>`); claude's bare
    # `-r` opens its own interactive picker instead.
    flags: dict[str, list[str]] = field(default_factory=dict)
    resume_requires_id: bool = False
    # Flag this harness uses to load an extra MCP config file at spawn (Phase
    # 4 Stage 2c — injecting the IDE's browser MCP server). claude takes
    # `--mcp-config <path>`; opencode has no per-launch flag (its MCP lives in
    # opencode.json), so it stays None until that path is wired.
    mcp_config_flag: str | None = None


HARNESSES: dict[str, AgentHarness] = {
    "claude": AgentHarness(
        name="claude",
        executable="claude",
        label="Claude",
        dangerous_flag="--dangerously-skip-permissions",
        flags={"fresh": [], "resume": ["-r"], "continue": ["-c"]},
        mcp_config_flag="--mcp-config",
    ),
    "opencode": AgentHarness(
        name="opencode",
        executable="opencode",
        label="OpenCode",
        # The full TUI's auto-approve is the OPENCODE_PERMISSION env var in
        # NESTED allow-all form (every tool, every pattern). A bare "allow"
        # or a config merge is too weak: OpenCode resolves permissions
        # last-match-wins by document order, and only the injected
        # top-level "*" lands last so it overrides explicit "ask" rules.
        # Verified empirically in orchestrator.nvim (terminal.lua) — do not
        # simplify to OPENCODE_CONFIG_CONTENT='{"permission":"allow"}'.
        dangerous_env=(("OPENCODE_PERMISSION", '{"*":{"*":"allow"}}'),),
        # `-c` continues the most recent session; `--session <ses_...>`
        # resumes a specific one. Bare `--session` (no id) errors — OpenCode
        # has no picker flag equivalent to claude's bare `-r`, so resume
        # goes through the IDE's session picker (AgentSessionPicker.qml).
        flags={"fresh": [], "resume": ["--session"], "continue": ["-c"]},
        resume_requires_id=True,
    ),
}


def spawn_argv(
    harness: AgentHarness,
    spawn_type: str,
    dangerous: bool,
    agent_id: str,
    session_id: str = "",
    mcp_config_path: str = "",
) -> list[str]:
    """argv for a slot's QMLTermSession.

    `env`-wrapper because KSession::setEnvironment is not QML-reachable —
    same technique orchestrator.nvim's termopen uses. SYMMETRIA_AGENT_ID
    is what BOTH activity reporters key on: claude's hooks
    (symmetria-agent-hook.py) and opencode's plugin
    (~/.config/opencode/plugin/symmetria-agent.js) read it and report to
    the bridge under that id.

    `mcp_config_path`, when set AND the harness declares `mcp_config_flag`,
    appends `<flag> <path>` so the agent discovers the IDE's browser MCP
    server (Stage 2c). Empty path or a harness without the flag (opencode
    today) is a no-op.
    """
    argv = ["env", f"SYMMETRIA_AGENT_ID={agent_id}"]
    if dangerous:
        argv += [f"{key}={value}" for key, value in harness.dangerous_env]
    argv.append(harness.executable)
    if dangerous and harness.dangerous_flag:
        argv.append(harness.dangerous_flag)
    if mcp_config_path and harness.mcp_config_flag:
        argv += [harness.mcp_config_flag, mcp_config_path]
    argv += harness.flags[spawn_type]
    if spawn_type == "resume" and harness.resume_requires_id:
        argv.append(session_id)
    return argv


def parse_opencode_sessions(stdout: str) -> list[dict] | None:
    """Parse `opencode session list --format json` into picker rows.

    Returns rows shaped {id, title, when} sorted newest-first, or None
    when the output isn't a JSON array (distinct from [] = genuinely no
    sessions). `updated`/`created` are epoch milliseconds.
    """
    try:
        decoded = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(decoded, list):
        return None

    def session_ts(session: dict) -> int | float | None:
        ts = session.get("updated") or session.get("created")
        return ts if isinstance(ts, (int, float)) else None

    # Filter BEFORE sorting — sort_key runs on every element, so a
    # non-dict entry in an otherwise valid array would raise there.
    sessions = [s for s in decoded if isinstance(s, dict)]
    rows = []
    for session in sorted(sessions, key=lambda s: session_ts(s) or 0, reverse=True):
        session_id = str(session.get("id") or "")
        if not session_id:
            continue
        ts = session_ts(session)
        when = ""
        if ts is not None:
            when = time.strftime("%b %d %H:%M", time.localtime(ts / 1000))
        rows.append(
            {
                "id": session_id,
                "title": str(session.get("title") or "") or session_id,
                "when": when,
            }
        )
    return rows
