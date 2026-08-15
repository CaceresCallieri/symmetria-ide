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

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field

# Ctrl+U/D scroll fraction for a harness that declares none, and the value
# every mouse-tracking TUI uses. A module constant rather than a read of
# `AgentHarness.scroll_page_fraction`: `slots=True` replaces class-level field
# defaults with slot descriptors, so that read would return a descriptor.
DEFAULT_SCROLL_PAGE_FRACTION = 0.167


@dataclass(slots=True, frozen=True)
class AgentHarness:
    """Per-CLI spawn semantics for one agent harness.

    The permissive spawn variant is expressed differently per harness: a
    launch flag for claude (`dangerous_flag`), an env var for opencode
    (`dangerous_env`) — the bare opencode TUI accepts no permission flag.

    ⚠ It does not mean the same THING everywhere. For claude and opencode it
    genuinely skips tool permissions. Pi has no per-tool permission gate at
    all (only `--no-tools`/`--tools`/`--exclude-tools`), so both of its
    variants are equally unrestricted on tools and its `--approve` only
    suppresses the blocking project-trust dialog. Do not describe the Pi
    variants as a permissions difference — see the `pi` entry below.
    """

    name: str
    executable: str
    label: str
    # ---- Spawn-chooser metadata (AgentSpawnMenu's stage-0 harness rows) ----
    #
    # REQUIRED, deliberately: the two-stage chooser projects its rows straight
    # out of this registry (AppController.agentHarnessCatalog), so a harness
    # that omitted them would render a keyless, iconless, unselectable row.
    # Declaring them here is what makes "a fourth harness is a registry entry
    # plus an icon" true rather than aspirational — there is no parallel UI
    # table in app.py or QML to keep in sync.
    #
    # The single-letter selection key, UPPERCASE. Uppercase because that is
    # what Qt's key enum carries (`Qt.Key_P` == 0x50 == ord("P")), letting the
    # menu dispatch by `menuKey.charCodeAt(0) === event.key` with no per-
    # harness branch. Must be unique across the registry.
    menu_key: str
    # Brand mark, as a path RELATIVE TO qml/ (the menu resolves it against its
    # own directory). Brand fills are baked into the SVG — intentionally NOT
    # Theme-tokened, so the logo identifies the backend regardless of theme.
    icon: str
    # Stage-1 label for the `r` row. It differs per harness because the resume
    # UX does: claude and pi open their OWN interactive picker inside the
    # terminal, opencode has no picker flag and defers to the IDE's
    # AgentSessionPicker (see `resume_requires_id`).
    resume_label: str
    # Presentation order in the chooser, ascending. Not the dict order: the
    # registry is keyed for lookup, this decides what the user sees first.
    menu_order: int
    # Flags appended to EVERY spawn of this harness, right after the
    # executable. claude carries `--no-chrome`: with the user's global
    # `claudeInChromeDefaultEnabled`, every claude session auto-connects the
    # Claude-in-Chrome extension MCP (the user's REAL Chrome). Inside the IDE
    # that breaks the browser containment principle — agents open tabs in an
    # escaped Hyprland window with no chip globe / Ctrl+Shift+B / attribution,
    # and several concurrent agents sharing the one extension is flaky. IDE
    # agents must only ever see the injected symmetria-browser/chrome-devtools
    # servers, so the integration is disabled per-invocation, unconditionally.
    base_flags: tuple[str, ...] = ()
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
    # The ONLY flag resume-by-id is ever built from. Harnesses whose picker
    # flag and resume-by-id flag COINCIDE (claude `-r`, opencode `--session`)
    # simply name that same flag here, so their argv is unchanged. Pi is why
    # the field exists: its picker is a bare `-r`, but a trailing token after
    # `-r` is NOT read as a session id — Pi's arg parser pushes any non-flag
    # token onto `result.messages` (dist/cli/args.js:194), so `pi -r <uuid>`
    # opens the picker AND queues the uuid as the session's first user
    # message. Silent corruption, not an error.
    #
    # ⚠ There is deliberately NO fallback to "append the id after the picker
    # flags". That shape IS the corruption above, so a harness that reaches
    # `spawn_argv` with a session id and no `resume_id_flag` raises instead of
    # guessing (see the raise there, and the registry invariant test). A new
    # harness that can restore a persisted session must name its flag here; one
    # that genuinely cannot (picker-only) must never be handed a session id.
    resume_id_flag: str | None = None
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
    # Env var this harness's IDE integration reads an MCP config FILE PATH from
    # (the third delivery form, distinct from both siblings above: `*_flag`
    # passes a path as a CLI flag, `*_env` passes inline CONTENT in the env,
    # this one passes a PATH in the env). Pi ships zero MCP support of its own
    # — verified, `modelcontextprotocol` appears nowhere in its dist — so the
    # Symmetria Pi extension acts as a generic MCP client and reads the very
    # same per-agent config file claude gets. Because the file is identical,
    # per-project gating, call-time gating and X-Symmetria-Agent attribution
    # all keep working with no new IDE-side tool surface.
    mcp_config_path_env: str | None = None
    # Flag this harness uses to load an extra extension/plugin FILE at spawn.
    # Pi takes `--extension <path>`; used only by the dev override below, since
    # production Pi auto-discovers the extension from its registered package.
    extension_flag: str | None = None
    # Env var holding a DEV override path for that extension. The globally
    # registered Pi package is `pi-agent-stable`, so an unpromoted extension in
    # a dev worktree is otherwise unreachable — pointing this at the worktree
    # file lets the IDE exercise it without a promotion.
    extension_path_env: str | None = None
    # Fraction of the visible pane Ctrl+U/D scrolls for this harness. THIS
    # FIELD IS THE SINGLE AUTHORITY for the value — QML and the AppController
    # Slot only forward it. Not one constant, because the effective jump
    # depends on the TUI, not the terminal: a mouse-tracking program (claude)
    # consumes each emitted wheel event as several of its own lines, so 0.167
    # lands near a real half page there while a literal 0.5 overshoots to ~1.5
    # pages. Retune per harness here, nowhere else.
    scroll_page_fraction: float = DEFAULT_SCROLL_PAGE_FRACTION
    # Slash command that opens this harness's OWN model picker (Alt+M composes
    # the CLI's picker rather than reimplementing one in QML). None = the
    # harness has no such command and the chord is a no-op there (opencode).
    model_slash_command: str | None = None
    # Can the coordination judge read this harness's transcript? `wait_for_agent`
    # (see AppController's coordination section) verifies a watched agent's
    # busy→idle edge by running a headless `claude -p` over the transcript the
    # agent wrote. That needs BOTH a known on-disk location and a known JSONL
    # shape, which today only claude has. False = the registrant still gets its
    # go-ahead, with an explicit caveat that no verification happened.
    #
    # A capability, deliberately NOT a `name == "claude"` test: Pi writes a
    # genuine JSONL transcript at `getSessionFile()`, so teaching the judge to
    # read it is a matter of flipping this flag plus a reader — not of hunting
    # down literal harness-name branches. Adding a harness therefore defaults to
    # the honest, caveated path rather than to a silently wrong verification.
    judgeable_transcript: bool = False
    # Bare product names this harness reports as its OSC title before a session
    # has a real summary. Write them in their natural casing ("Claude Code") —
    # matching is case-INSENSITIVE by construction (AppController lowercases
    # the whole set when aggregating it), so casing here is documentation only.
    # `_clean_agent_title` treats a match as NO title, so the chip shows just
    # the sparkle + slot number.
    title_placeholders: tuple[str, ...] = ()
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

    @property
    def wants_mcp_config_file(self) -> bool:
        """Does this harness consume the per-agent MCP config FILE?

        True for both file-delivery forms — `mcp_config_flag` (claude reads the
        path as a CLI flag) and `mcp_config_path_env` (pi's extension reads the
        path out of the env). Explicitly NOT the opencode route, which takes
        inline CONTENT via `mcp_config_env` and needs no file written at all.

        One named predicate rather than a repeated `flag or path_env` test at
        the caller: `spawn_argv` deliberately keeps the two delivery branches
        separate (they place the value differently), but the DECISION to build
        the config file at all is a single capability question. A future fourth
        delivery mechanism adds itself here and cannot be silently omitted from
        the build-config decision.
        """
        return bool(self.mcp_config_flag or self.mcp_config_path_env)


# `env -u` pairs prepended to EVERY spawn's env wrapper, scrubbing the ambient
# Claude-session environment an agent must never inherit. Two leak paths feed
# the ambient env, both real:
#
# 1. IDE launched from inside a Claude Code session (the standard dev loop —
#    an agent in the stable IDE starts the dev IDE): direct-PTY children
#    inherit the IDE's env.
# 2. tmux substrate: session commands inherit the TMUX SERVER's environment —
#    the env of whoever first touched the shared socket. Verified live
#    2026-07-13: the server had been started from inside a vigilia agent, so
#    EVERY spawned agent across ALL projects inherited that agent's session env.
#
# Per-var consequences (all observed, not theoretical):
# - CLAUDE_CODE_CHILD_SESSION / CLAUDE_CODE_SESSION_ID: claude SILENTLY SKIPS
#   persisting the transcript to ~/.claude/projects/<proj>/<session>.jsonl
#   (verified 2026-07-03) — breaks the coordination judge and the shell hook's
#   last-message digests.
# - CLAUDE_JOB_DIR: the new claude ADOPTS the leaked session's job
#   (~/.claude/jobs/<id>/) and NAMES its session after that job's `name` —
#   fresh agents in project B were born titled with project A's session title,
#   poisoning the resume picker of every project on the socket (verified
#   2026-07-13; the "mesura.consulting … (Branch)" incident).
# - CLAUDE_EFFORT: silently pins the leaked session's effort on agents whose
#   project sets no `.symmetria/ide.json` effort default.
# - CLAUDECODE / CLAUDE_CODE_ENTRYPOINT / CLAUDE_CODE_EXECPATH: nested-claude
#   markers; a top-level launch must not look like a child of the leaked
#   session. claude re-sets them itself on startup.
#
# IDE-spawned agents are always top-level user sessions — unset unconditionally
# (env -u on an absent var is a no-op).
CLAUDE_ENV_UNSET_ARGS: tuple[str, ...] = (
    "-u",
    "CLAUDE_CODE_CHILD_SESSION",
    "-u",
    "CLAUDE_CODE_SESSION_ID",
    "-u",
    "CLAUDE_JOB_DIR",
    "-u",
    "CLAUDE_EFFORT",
    "-u",
    "CLAUDECODE",
    "-u",
    "CLAUDE_CODE_ENTRYPOINT",
    "-u",
    "CLAUDE_CODE_EXECPATH",
)

HARNESSES: dict[str, AgentHarness] = {
    "claude": AgentHarness(
        name="claude",
        executable="claude",
        label="Claude",
        menu_key="C",
        icon="assets/claude-icon.svg",
        # Bare `-r` opens claude's own interactive picker in the terminal.
        resume_label="resume (claude's picker)",
        menu_order=1,
        base_flags=("--no-chrome",),
        dangerous_flag="--dangerously-skip-permissions",
        flags={"fresh": [], "resume": ["-r"], "continue": ["-c"]},
        # Picker flag and resume-by-id flag coincide for claude — naming it
        # here keeps the argv byte-identical while making the semantics
        # explicit rather than implied by the trailing-append fallback.
        resume_id_flag="-r",
        mcp_config_flag="--mcp-config",
        settings_flag="--settings",
        model_slash_command="/model",
        # The only harness whose transcript the coordination judge can read
        # today: `~/.claude/projects/<slug>/<session_id>.jsonl`, a format the
        # judge prompt already understands.
        judgeable_transcript=True,
        # Natural casing on purpose — matching is case-insensitive, and the
        # string reads as what claude actually prints.
        title_placeholders=("Claude Code",),
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
        menu_key="O",
        icon="assets/opencode-icon.svg",
        # The one harness whose resume goes through the IDE's own picker —
        # `--session` requires an id and opencode ships no picker flag.
        resume_label="resume (session picker)",
        menu_order=2,
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
        # Same flag either way (bare `--session` errors, so the picker form is
        # never actually spawned — the QML session picker always supplies one).
        resume_id_flag="--session",
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
        title_placeholders=("opencode",),
    ),
    "pi": AgentHarness(
        name="pi",
        # The INSTALLED binary, never a repo checkout: /usr/bin/pi is the
        # runtime and it loads its packages from ~/.pi/agent/settings.json.
        executable="pi",
        label="Pi",
        menu_key="P",
        icon="assets/pi-icon.svg",
        # Like claude, pi's bare `-r` opens its own picker inside the pane —
        # no IDE-side session list (see `resume_requires_id=False` below).
        resume_label="resume (pi's picker)",
        # First in the chooser: the harness this integration exists to make
        # reachable, and the one whose row the user is looking for.
        menu_order=0,
        # ⚠ NOT a skip-permissions flag, and must not be documented as one.
        # `--approve` sets `projectTrustOverride` (dist/cli/args.js:163) —
        # it suppresses the blocking "Project is not trusted" startup dialog,
        # which would otherwise wedge a freshly spawned slot in a new project.
        # Pi has NO per-tool permission gate at all (only --no-tools/--tools/
        # --exclude-tools), so both spawn variants are equally unrestricted on
        # tools; they differ in project-resource trust only.
        dangerous_flag="--approve",
        # Bare `-r` opens Pi's own interactive picker (no id, no IDE picker);
        # resume-by-id goes through `resume_id_flag` — see that field's comment
        # for why the id must NOT simply be appended after `-r`.
        flags={"fresh": [], "resume": ["-r"], "continue": ["-c"]},
        resume_id_flag="--session",
        resume_requires_id=False,
        mcp_config_path_env="SYMMETRIA_IDE_MCP_CONFIG",
        extension_flag="--extension",
        extension_path_env="SYMMETRIA_IDE_PI_EXTENSION",
        # `--model <id>`; `--thinking <level>` is Pi's reasoning-effort axis.
        # `off` is a legal level, not an "unset" sentinel — an explicit `off`
        # in the committed marker is honoured and emitted.
        model_flag="--model",
        effort_flag="--thinking",
        valid_efforts=frozenset(
            {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
        ),
        # ⚠ PREDICTED, NOT YET MEASURED. Derivation: pi emits none of the
        # ?1000/?1002/?1006/?1049 mouse-tracking / alternate-screen modes, so
        # it should take Konsole's plain _scrollBar branch where the fraction
        # is literal (0.5 = half a page) instead of claude's line-multiplying
        # wheel-event path. The live phase measures a real pi pane and locks or
        # retunes this — do not treat 0.5 as an observed value until then.
        scroll_page_fraction=0.5,
        model_slash_command="/model",
        # ⚠ PREDICTED, NOT YET MEASURED: pi's actual OSC title string has not
        # been captured from a running slot. The live phase reads `on_agent_title`
        # off a real pi agent and finalises this tuple against it. A wrong guess
        # is cosmetic only (the chip shows the raw placeholder as if it were a
        # session summary), never a spawn failure.
        title_placeholders=("pi",),
    ),
}

# The registry in the order the spawn chooser shows it. Sorted ONCE, here, at
# import: `HARNESSES` is keyed for lookup and its dict order is incidental, so
# every consumer that wants presentation order used to sort it again — and the
# catalog projection did that on every QML read, including one full sort per
# single-harness lookup. The set is compile-time constant, so the sort has no
# reason to run more than once in the process.
MENU_ORDERED_HARNESSES: tuple[AgentHarness, ...] = tuple(
    sorted(HARNESSES.values(), key=lambda harness: harness.menu_order)
)


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
    extension_paths: tuple[str, ...] = (),
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
    the opencode counterpart (inline JSON, no file). `mcp_config_path` ALSO
    serves the third form: a harness declaring `mcp_config_path_env` gets the
    same file's PATH exported in the env wrapper (pi, whose extension reads it
    as a generic MCP client). A given harness uses one mechanism only; the
    unused arg (and either arg on a harness lacking the corresponding
    flag/env) is a silent no-op.

    `extension_paths`, when non-empty AND the harness declares
    `extension_flag`, appends `<flag> <path>` per entry — the dev override for
    an unpromoted Pi extension.

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
    argv = ["env", *CLAUDE_ENV_UNSET_ARGS, f"SYMMETRIA_AGENT_ID={agent_id}"]
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
        # Second capability advert, deliberately SEPARATE from the tap one:
        # this IDE renders account usage in its own status bar (UsageIndicator),
        # so `status-line.sh` omits its 5h/7d segment here rather than printing
        # the same numbers twice. It must not ride the tap var — the stable
        # build sets THAT one but has no panel, and gating on it would blank the
        # usage display in the user's daily driver until this promotes.
        argv.append("SYMMETRIA_IDE_USAGE_PANEL=1")
    if dangerous:
        argv += [f"{key}={value}" for key, value in harness.dangerous_env]
    # Inline MCP config env (opencode): rides the env wrapper like dangerous_env.
    # One argv element — no shell involved (KSession execs argv directly), so the
    # JSON needs no quoting, same as OPENCODE_PERMISSION above.
    if mcp_config_content and harness.mcp_config_env:
        argv.append(f"{harness.mcp_config_env}={mcp_config_content}")
    # Config-file PATH in the env (pi): the same file claude gets via
    # --mcp-config, handed to Pi's Symmetria extension instead — Pi's own CLI
    # has no MCP surface to pass it to.
    if mcp_config_path and harness.mcp_config_path_env:
        argv.append(f"{harness.mcp_config_path_env}={mcp_config_path}")
    # `executable` overrides the bare harness name with an ABSOLUTE path when the
    # caller has resolved one (tmux mode — so finding the CLI does not depend on
    # the tmux server's inherited PATH). Empty = use the bare name (legacy PTY,
    # where the child inherits the IDE's own PATH).
    argv.append(executable or harness.executable)
    argv += harness.base_flags
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
    # Dev-override extension(s), before the spawn-type flags so a resume's
    # trailing session id stays last.
    if extension_paths and harness.extension_flag:
        for path in extension_paths:
            argv += [harness.extension_flag, path]
    # Resume-by-id vs the interactive picker. With an id, `resume_id_flag`
    # ALONE builds the resume — for claude (`-r`) and opencode (`--session`)
    # it is the same flag the picker uses, so their argv is unchanged; pi
    # swaps its bare-`-r` picker for `--session <id>`. Without an id we emit
    # the picker flags. Gating on a truthy session_id (not `resume_requires_id`)
    # is what keeps claude's bare `-r` picker reachable.
    #
    # No id + no flag fallback: combining picker syntax with a trailing id is
    # precisely the `pi -r <uuid>` corruption this field exists to prevent, and
    # it fails SILENTLY (the id becomes a chat message), so a harness that
    # cannot express resume-by-id must say so by never being handed an id.
    if spawn_type == "resume" and session_id:
        if not harness.resume_id_flag:
            raise ValueError(
                f"harness {harness.name!r} was asked to resume session id "
                f"{session_id!r} but declares no resume_id_flag; refusing to "
                "append the id to its picker flags "
                f"({harness.flags.get('resume', [])!r}) — that shape is read as "
                "a chat message by at least one CLI, not as a session id"
            )
        argv += [harness.resume_id_flag, session_id]
    else:
        argv += harness.flags[spawn_type]
    return argv


def tmux_session_name(project_root: str, slot: int) -> str:
    """Collision-free tmux session name for a slot: ``<slug>-<pathhash>-<slot>``.

    Under the project→agents model (the phone/IDE group sessions by project), the
    tmux session name is an INTERNAL id, NOT the user-facing label — the UI shows
    the project name (from the pane's cwd) and the agent's own name, both from
    metadata. So this name only has to be UNIQUE and STABLE, not pretty:

    - ``slug`` is the project basename reduced to ``[a-z0-9-]`` (kept purely for
      human debuggability; tmux also forbids ``.``/``:`` in session names).
    - a short hash of the FULL ABSOLUTE path disambiguates distinct roots that
      share a basename (``~/a/app`` vs ``~/b/app``) — they must never collide on
      the shared socket, where ``new-session -A`` would otherwise ATTACH the wrong
      project's live session instead of creating a new one. The path is coerced to
      absolute so the hash is stable regardless of the caller's cwd.
    - it is deterministic (same path → same name, no pid), so a restarted IDE (or
      ``new-session -A``) still re-adopts the surviving session.

    6 hex chars (~16M values) is generous for interactive single-user use; revisit
    the width only if the number of concurrently tracked project roots ever nears
    the thousands (birthday-bound collision territory). A rootless/empty path slugs
    to ``agent``.
    """
    normalized = os.path.normpath(project_root or "")
    base = os.path.basename(normalized)
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "agent"
    digest = hashlib.sha1(os.path.abspath(normalized).encode()).hexdigest()[:6]
    return f"{slug}-{digest}-{slot}"


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


def format_session_when(epoch_seconds: float | None) -> str:
    """The picker's short timestamp label; "" when there is no timestamp.

    Shared so the two producers of a picker row — this module's parser and
    `AppController`'s projection of the thread index — cannot format the same
    column two different ways.
    """
    if epoch_seconds is None:
        return ""
    return time.strftime("%b %d %H:%M", time.localtime(epoch_seconds))


def parse_opencode_sessions(stdout: str) -> list[dict] | None:
    """Parse `opencode session list --format json` into normalized rows.

    Returns rows sorted newest-first, or None when the output isn't a JSON
    array (distinct from [] = genuinely no sessions).

    A row carries BOTH halves: `{id, title, when}` for the resume picker, and
    `{directory, projectId, updated, created}` for `OpenCodeThreadReader`,
    which needs the scope fields to decide whether a session belongs to this
    project at all. The two used to be separate — the picker's shape was all
    this returned, and the reader would have had to re-parse the same JSON to
    recover `directory`. `updated`/`created` stay in the CLI's own unit, epoch
    MILLISECONDS, and are 0 when absent.
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
        rows.append(
            {
                "id": session_id,
                "title": str(session.get("title") or "") or session_id,
                "when": format_session_when(None if ts is None else ts / 1000),
                "directory": str(session.get("directory") or ""),
                "projectId": str(session.get("projectId") or ""),
                "updated": _epoch_ms(session.get("updated")),
                "created": _epoch_ms(session.get("created")),
            }
        )
    return rows


def _epoch_ms(value: object) -> int:
    """One `updated`/`created` field as epoch milliseconds; 0 when absent."""
    return int(value) if isinstance(value, (int, float)) else 0
