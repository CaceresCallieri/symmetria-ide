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
    # spawn_type -> CLI flags. For `resume`, the session id is appended after
    # the flag whenever one is supplied (opencode `--session <id>` REQUIRES it;
    # claude `-r <id>` resumes non-interactively). A bare claude `-r` (no id)
    # opens claude's own interactive picker.
    flags: dict[str, list[str]] = field(default_factory=dict)
    resume_requires_id: bool = False
    # Flag this harness uses to load an extra MCP config FILE at spawn (Phase
    # 4 Stage 2c — injecting the IDE's browser MCP server). claude takes
    # `--mcp-config <path>`. opencode has no per-launch flag, so it stays None
    # and injects via `mcp_config_env` instead (below).
    mcp_config_flag: str | None = None
    # Env var this harness reads INLINE MCP config from at spawn (the opencode
    # counterpart to `mcp_config_flag` — same claude-flag/opencode-env asymmetry
    # as dangerous_flag vs dangerous_env). opencode has no `--mcp-config` flag;
    # its config comes from files + `OPENCODE_CONFIG_CONTENT`, which DEEP-MERGES
    # inline JSON over the project's opencode.json at highest precedence (so our
    # browser servers add to, never clobber, the project's own). claude stays
    # None (it uses the file+flag form). Verified via spike (2026-07-01) — see
    # .claude/memory/reference/agent-sdk/opencode_remote_mcp_sse_only.md.
    mcp_config_env: str | None = None
    # Flag this harness uses to load EXTRA settings at spawn (agent-ownership
    # inversion — injecting the IDE-owned activity-reporter hook). claude takes
    # `--settings <file-or-json>` and we pass an inline JSON string (verified:
    # `claude --help` documents the json form), so no temp file is needed — the
    # settings are identical for every agent (the reporter learns its per-agent
    # identity from SYMMETRIA_AGENT_ID in the env, not from the settings).
    # opencode has no equivalent per-launch flag, so it stays None (its agents
    # keep reporting to the shell bridge — a known Phase-1 gap).
    settings_flag: str | None = None


# `env -u` pairs prepended to EVERY spawn's env wrapper: when the IDE itself
# was launched from inside a Claude Code session (the standard dev loop — an
# agent in the stable IDE starts the dev IDE), spawned agents would inherit
# CLAUDE_CODE_CHILD_SESSION=1 + CLAUDE_CODE_SESSION_ID, and claude then
# SILENTLY SKIPS persisting the session transcript to
# ~/.claude/projects/<proj>/<session>.jsonl (verified live 2026-07-03). That
# breaks the coordination judge ("transcript not found") AND the shell hook's
# last-message digests. IDE-spawned agents are always top-level user sessions,
# never children — unset unconditionally (env -u on an absent var is a no-op).
CHILD_SESSION_UNSET_ARGS: tuple[str, ...] = (
    "-u",
    "CLAUDE_CODE_CHILD_SESSION",
    "-u",
    "CLAUDE_CODE_SESSION_ID",
)

HARNESSES: dict[str, AgentHarness] = {
    "claude": AgentHarness(
        name="claude",
        executable="claude",
        label="Claude",
        dangerous_flag="--dangerously-skip-permissions",
        flags={"fresh": [], "resume": ["-r"], "continue": ["-c"]},
        mcp_config_flag="--mcp-config",
        settings_flag="--settings",
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
        # opencode's browser MCP is injected as inline JSON via this env var
        # (it has no --mcp-config flag). The IDE builds the content with
        # browser_mcp.agent_config_content (opencode `mcp`-key schema).
        mcp_config_env="OPENCODE_CONFIG_CONTENT",
    ),
}


def spawn_argv(
    harness: AgentHarness,
    spawn_type: str,
    dangerous: bool,
    agent_id: str,
    session_id: str = "",
    mcp_config_path: str = "",
    mcp_config_content: str = "",
    settings_json: str = "",
    agent_sock_path: str = "",
) -> list[str]:
    """argv for a slot's QMLTermSession.

    `env`-wrapper because KSession::setEnvironment is not QML-reachable —
    same technique orchestrator.nvim's termopen uses. SYMMETRIA_AGENT_ID
    is what BOTH activity reporters key on: claude's IDE-owned reporter
    (runtime/symmetria-ide-agent-hook.py) and opencode's plugin
    (~/.config/opencode/plugin/symmetria-agent.js) read it to tag their
    reports with the agent's `<ide_pid>_<slot>` id.

    `mcp_config_path`, when set AND the harness declares `mcp_config_flag`,
    appends `<flag> <path>` so the agent discovers the IDE's browser MCP
    server (Stage 2c — claude). `mcp_config_content`, when set AND the harness
    declares `mcp_config_env`, exports `<env>=<content>` in the env wrapper —
    the opencode counterpart (inline JSON, no file). A given harness uses one
    mechanism or the other; the unused arg (and either arg on a harness lacking
    the corresponding flag/env) is a silent no-op.

    `agent_sock_path` (when set) exports `SYMMETRIA_IDE_AGENT_SOCK` so the
    claude reporter knows which IDE socket to report to; `settings_json`
    (when set AND the harness declares `settings_flag`) appends
    `<settings_flag> <json>` to REGISTER that reporter as a claude hook
    (agent-ownership inversion). Both empty / a harness without the flag
    (opencode) is a no-op — opencode keeps reporting to the shell bridge.
    """
    argv = ["env", *CHILD_SESSION_UNSET_ARGS, f"SYMMETRIA_AGENT_ID={agent_id}"]
    # Exported unconditionally when provided (harmless for opencode, which has no
    # reporter reading it); the settings registration below is what actually
    # wires claude's hook to this socket.
    if agent_sock_path:
        argv.append(f"SYMMETRIA_IDE_AGENT_SOCK={agent_sock_path}")
        # Capability advert: this IDE renders the Claude status-line tap natively
        # (model/effort/context%/usage). The global ~/.claude/status-line.sh gates
        # its tap on this var, so an IDE on OLDER code (e.g. a not-yet-promoted
        # stable build that lacks the `status_line` handler) never gets tapped —
        # it would otherwise log every tap as an "unmapped" activity event. Rides
        # with the sock (the tap needs the socket to send to). Inert for opencode
        # (no opencode status-line integration yet).
        argv.append("SYMMETRIA_IDE_STATUSLINE_TAP=1")
    if dangerous:
        argv += [f"{key}={value}" for key, value in harness.dangerous_env]
    # Inline MCP config env (opencode): rides the env wrapper like dangerous_env.
    # One argv element — no shell involved (KSession execs argv directly), so the
    # JSON needs no quoting, same as OPENCODE_PERMISSION above.
    if mcp_config_content and harness.mcp_config_env:
        argv.append(f"{harness.mcp_config_env}={mcp_config_content}")
    argv.append(harness.executable)
    if dangerous and harness.dangerous_flag:
        argv.append(harness.dangerous_flag)
    if mcp_config_path and harness.mcp_config_flag:
        argv += [harness.mcp_config_flag, mcp_config_path]
    if settings_json and harness.settings_flag:
        argv += [harness.settings_flag, settings_json]
    argv += harness.flags[spawn_type]
    # Resume-by-id: append the session id after the resume flag when one is
    # supplied. opencode REQUIRES it (bare `--session` errors). claude takes
    # it OPTIONALLY — `-r <id>` resumes that exact session non-interactively
    # (what session restore replays to re-home a conversation), while a bare
    # `-r` (empty id) opens claude's own interactive picker. Gating on a
    # truthy session_id (not `resume_requires_id`) is what unlocks claude's
    # non-interactive resume without changing opencode (its picker always
    # supplies an id).
    if spawn_type == "resume" and session_id:
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
