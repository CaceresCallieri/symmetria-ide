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
import os
import re
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
    # Per-project model / reasoning-effort defaults (read from the committed
    # `.symmetria/ide.json` marker by project_browser_marker.harness_model_effort,
    # threaded through spawn_argv). Both are plain launch flags that take a value
    # after them — claude `--model <alias|id> --effort <level>`, opencode
    # `--model <provider/model> --variant <provider-effort>`. None = the harness
    # has no such flag, so the corresponding default is a silent no-op.
    model_flag: str | None = None
    effort_flag: str | None = None
    # Legal effort values for this harness. A NON-EMPTY set turns spawn_argv into
    # a validator: a committed `effort` outside the set is dropped (the agent
    # launches at the harness default) rather than passed through to crash the
    # launch — the marker is committable and shared, so a teammate's typo must
    # not break spawns. An EMPTY set means "don't validate" (opencode's
    # `--variant` is provider-specific and open-ended, so any string passes).
    valid_efforts: frozenset[str] = frozenset()


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
        # `--model` takes an alias ('fable'/'opus'/'sonnet') OR a full id;
        # `--effort` takes one of the five levels below (verified via
        # `claude --help`: "Effort level ... (low, medium, high, xhigh, max)").
        model_flag="--model",
        effort_flag="--effort",
        valid_efforts=frozenset({"low", "medium", "high", "xhigh", "max"}),
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
        # `-m/--model` takes `provider/model`; `--variant` is opencode's
        # "provider-specific reasoning effort" (verified via `opencode run
        # --help`). valid_efforts stays EMPTY: the accepted variant values are
        # provider-defined, not a fixed enum we can validate against, so any
        # committed string passes through.
        model_flag="--model",
        effort_flag="--variant",
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
    model: str = "",
    effort: str = "",
    executable: str = "",
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

    `model` / `effort`, when set AND the harness declares the matching flag,
    append `<model_flag> <model>` / `<effort_flag> <effort>` — the per-project
    launch defaults from `.symmetria/ide.json`. `effort` is validated against
    the harness's `valid_efforts` (when non-empty): an out-of-set value is
    DROPPED (launch at harness default) rather than passed through to crash the
    spawn — the committable marker must tolerate a teammate's typo. `model` is
    passed through as-is (aliases and full ids are both valid, and the valid-id
    set drifts per release, so validating it here would rot).

    `executable`, when set, replaces the bare `harness.executable` with an
    absolute path (see `tmux_wrap` — the tmux server's PATH may differ from the
    IDE's). Empty keeps the bare name.
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
    # `executable` overrides the bare harness name with an ABSOLUTE path when the
    # caller has resolved one (tmux mode — so finding the CLI does not depend on
    # the tmux server's inherited PATH). Empty = use the bare name (legacy PTY,
    # where the child inherits the IDE's own PATH).
    argv.append(executable or harness.executable)
    if dangerous and harness.dangerous_flag:
        argv.append(harness.dangerous_flag)
    if mcp_config_path and harness.mcp_config_flag:
        argv += [harness.mcp_config_flag, mcp_config_path]
    if settings_json and harness.settings_flag:
        argv += [harness.settings_flag, settings_json]
    # Per-project launch defaults. Order among option flags is irrelevant to
    # both CLIs; kept before the spawn-type flags so resume's trailing
    # session_id stays last. effort is validated against the harness's known
    # set (empty set = accept anything, e.g. opencode's provider-specific
    # --variant); an out-of-set value is skipped so a bad committed marker
    # degrades to the default effort instead of failing the launch.
    if model and harness.model_flag:
        argv += [harness.model_flag, model]
    if (
        effort
        and harness.effort_flag
        and (not harness.valid_efforts or effort in harness.valid_efforts)
    ):
        argv += [harness.effort_flag, effort]
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


def tmux_session_name(project_root: str, slot: int) -> str:
    """Stable tmux session name for a slot: ``<project-slug>-<slot>``.

    This name is what external clients (Vigilia's ttyd → the phone) LIST, so it
    must be human-meaningful AND stable across IDE restarts — no pid — so that a
    restarted IDE (or ``new-session -A``) re-adopts the surviving session instead
    of double-spawning. The slug is the project basename reduced to ``[a-z0-9-]``
    (tmux forbids ``.``/``:`` in session names); a rootless/empty path falls back
    to ``agent``.
    """
    base = os.path.basename(os.path.normpath(project_root or ""))
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "agent"
    return f"{slug}-{slot}"


def tmux_wrap(
    inner_argv: list[str],
    socket: str,
    session_name: str,
    conf_path: str = "",
    start_directory: str = "",
) -> list[str]:
    """Wrap an agent launch argv so it runs inside a tmux session on ``socket``.

    ``new-session -A`` = attach-if-exists, else create-and-run ``inner_argv`` — so
    a restarted IDE re-attaches the surviving agent rather than double-spawning;
    the inner ``env … <cli>`` command runs only on first creation. ``inner_argv``
    is appended LAST and verbatim: tmux stops option parsing at the first
    non-option (``env``) and execs the remainder directly (no shell), so the
    inline ``--settings <json>`` survives byte-for-byte (verified 2026-07-04,
    tmux 3.7). ``-f <conf>`` is a SERVER flag (before the command) and applies
    only when this invocation starts the server — harmless on a running one.

    ``start_directory`` (``-c``) is LOAD-BEARING when set: without it, the session
    command starts in the tmux *server's* cwd, not the project root. The agent's
    activity reporter keys on ``session_id`` + the agent's real ``os.getcwd()``
    (agent_registry), so a wrong cwd would misroute the agent's state — pass the
    project root here so it matches the pre-tmux direct-PTY behavior.
    """
    server_flags = ["-S", socket]
    if conf_path:
        server_flags += ["-f", conf_path]
    new_session_flags = ["-A"]
    if start_directory:
        new_session_flags += ["-c", start_directory]
    return [
        "tmux",
        *server_flags,
        "new-session",
        *new_session_flags,
        "-s",
        session_name,
        *inner_argv,
    ]


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
