# Directory-scoped MCP injection

> **Status: proposal / idea (2026-06-18).** Not committed, not scheduled. This captures a
> generic mechanism the IDE *could* own so that spawned agents receive different MCP servers
> depending on which project they open. Written after a concrete miss (Trello MCP failing to
> load under the IDE) exposed a structural gap. Revisit if/when per-project agent capabilities
> become a recurring need.

This document is intentionally written for a future agent with no prior context. It explains
why a shell-level solution cannot work for Symmetria IDE, and proposes an IDE-owned,
**MCP-agnostic** alternative. The motivating example is Trello, but nothing here is
Trello-specific.

## The problem, in one sentence

There is no way to say *"agents launched in projects under directory X should have MCP server Y,
and agents launched elsewhere should not"* — because the only layer that could make that
per-directory decision today (an interactive shell) is bypassed by how the IDE spawns agents.

## Background: the three MCP scopes Claude Code offers

Claude Code resolves MCP servers from exactly three scopes (verified against
https://code.claude.com/docs/en/mcp.md):

| Scope     | Stored in                                   | Applies to            |
| --------- | ------------------------------------------- | --------------------- |
| `local`   | `~/.claude.json`, keyed by absolute project path | that one project  |
| `project` | `.mcp.json` at the project root             | that one project      |
| `user`    | `~/.claude.json`, top-level `mcpServers`    | **every** project     |

Crucially, **Claude Code does not walk up the directory tree** — a `.mcp.json` in a parent
folder is invisible to subprojects. So "all projects under `chamba-hq/`" maps to no single
native scope: it is either *per-project* (N files / N registrations) or *global* (`user`),
nothing in between. There is no native "folder scope."

## Why the obvious fix (a shell wrapper) does not work here

The natural way to emulate folder scope is a shell function that, on `claude` launch, checks
`$PWD` and registers the server if it is under the target tree. That works for a human typing
`claude` in an interactive zsh shell — but **it does not work for the IDE**, because the IDE
never starts an interactive shell.

The spawn path is (file:line, current as of writing):

- `qml/Main.qml:1084-1105` — `Component.onCompleted` reads an argv from the controller and
  calls `agentSession.startShellProgram()`. `Main.qml:1072` sets the PTY's working directory to
  `controller.displayedRoot` (the opened project root).
- `src/symmetria_ide/app.py:2148-2183` — `agent_spawn_argv()` is the QML-facing slot.
- `src/symmetria_ide/agent_harness.py:80-113` — `spawn_argv()` builds the literal argv.

The resulting invocation is, effectively:

```
env SYMMETRIA_AGENT_ID=<slot> claude [--dangerously-skip-permissions]
```

executed via the Konsole PTY widget's `QProcess::start()` →
`~/projects/symmetria-qmltermwidget/lib/Pty.cpp:164-228` → `kprocess.cpp:255-260` → `execvp()`.

Two consequences follow directly from "the program is `env`, run via `execvp`, with no shell":

1. **No shell rc is sourced.** `~/.zshrc` (interactive only) is never read, so any `claude()`
   wrapper function defined there simply does not exist in this context and can never fire.
2. **Only the IDE process's own environment is inherited.** `env` adds `SYMMETRIA_AGENT_ID` and
   execs `claude`. Anything exported solely in `~/.zshrc` (e.g. credentials sourced from a
   secrets file there) is absent. Vars reach the agent only if the **IDE process itself** had
   them — i.e. they were set at the graphical-session level (e.g. `~/.zshenv`, which zsh sources
   for all invocations, or `~/.config/environment.d/`, or Hyprland `env =`).

This second point is a quiet trap even for the *per-project `.mcp.json`* fallback: a `.mcp.json`
that uses `${TRELLO_API_KEY}` expansion will expand to **empty** under the IDE unless the var is
present in the IDE's environment. So credential plumbing is part of this problem, not separate
from it.

## The key reframe

The shell wrapper was never the goal — it was one (incompatible) *trigger* for a write into
`~/.claude.json`. Claude Code reads that file **regardless of how it was launched**. So any
mechanism that writes the right `local`-scope entry (or a `project` `.mcp.json`) before the
agent boots achieves the same result under the IDE.

**The IDE is the correct place to own that trigger**, because it is the component that already:

- knows the target directory (`controller.displayedRoot`),
- constructs the spawn argv (`agent_harness.spawn_argv`), and
- controls the spawned environment (the `env …` prefix).

In other words, for IDE-launched agents the IDE *is* the shell. It should own the
folder→capability mapping that a shell rc would otherwise own.

## Proposal: an MCP-agnostic injection layer in the harness

Add a small, declarative, harness-level step that runs just before `spawn_argv` returns,
parameterized entirely by configuration — no server hardcoded in code.

### A. Declarative rule file

A user config (e.g. `~/.config/symmetria-ide/mcp-rules.toml`) mapping a path predicate to a set
of MCP server definitions:

```toml
# Each rule: if the opened project root matches `path_prefix`, ensure these servers exist
# for that project before the agent launches.
[[rule]]
path_prefix = "~/projects/chamba-hq"
servers     = ["trello"]

[servers.trello]
type    = "stdio"
command = "npx"
args    = ["-y", "@delorenj/mcp-server-trello"]
# Values may reference env (resolved from the IDE process env) or a secrets provider (below).
env     = { TRELLO_API_KEY = "${TRELLO_API_KEY}", TRELLO_TOKEN = "${TRELLO_TOKEN}" }
```

The server block is deliberately the canonical `.mcp.json` stdio shape, so it is portable and
mirrors what the existing `mcp-catalog` (consumed by the `install-mcp-*` skills in
`~/.dotfiles/.claude/skills/`) already stores. The two could even share a source of truth.

### B. Application strategy (pick one; they are not exclusive)

1. **Write `local` scope** into `~/.claude.json` for the project path before spawn
   (idempotent: skip if already present). Pro: no file in the user's repo; auto-trusted, no
   approval prompt. Con: the harness edits a global JSON file.
2. **Write/merge a `.mcp.json`** at the project root. Pro: portable, inspectable, team-shareable
   if committed. Con: creates a file in the repo (must be `.gitignore`d or intentionally
   committed); `project` scope normally prompts for approval, though the IDE already passes
   `--dangerously-skip-permissions`, which sidesteps that.
3. **Inject env only** into the `env …` argv prefix (e.g. append `TRELLO_API_KEY=…`). This
   solves credentials but NOT registration — a server definition must still exist via (1) or
   (2). Useful as the credential half of either.

Recommended default: **(1)** for registration + reading secrets from a provider (below) so no
plaintext secret is written to a repo file.

### C. Credential handling

Secrets must not land in committed files (this repo's sibling `mcp-catalog/mcps/gmail.md`
records a real leaked `GOCSPX-…` in a committed `.mcp.json` as the cautionary precedent). The
injection layer should resolve secrets from one of:

- the IDE process environment (requires the user to export them at session level, e.g.
  `~/.zshenv`), or
- a referenced secrets file read at spawn time (e.g. `~/.zsh_secrets`, mode `600`), or
- a future keyring/secret-service integration.

…and write the **resolved** value into the per-project `local` entry, which lives in
`~/.claude.json` (machine-local, never committed) — keeping the secret off any repo surface.

### D. Harness-agnostic, not just Claude

`agent_harness.py` already abstracts multiple harnesses (`HARNESSES[...]`). The injection step
should key off the *active harness's* config format (Claude → `~/.claude.json`/`.mcp.json`;
other harnesses → their own) so the feature generalizes beyond Claude Code.

## Alternatives considered (and why this is worth discussing anyway)

- **`user` scope (global).** One command, works under any launcher, zero maintenance — but the
  server then loads in *every* project (including the IDE's own dev sessions). Acceptable if the
  capability is cheap and broadly useful; wrong if it is project-confidential or context-noisy.
- **Per-project `.mcp.json` via the `install-mcp-*` skill (current fallback).** Explicit and
  team-shareable, but manual per project and subject to the credential-expansion trap above. Fine
  at low N; the friction is the new-project tax this proposal removes.
- **Do nothing.** Entirely reasonable until per-project agent capabilities recur often enough to
  justify a config surface. This document exists so that decision is made deliberately, not by
  omission.

## Open questions

1. Is per-project MCP differentiation a *recurring* need, or is Trello-for-chamba-hq a one-off
   better served by the manual skill?
2. Should the rule file be IDE-specific, or shared with the dotfiles `mcp-catalog` so the IDE and
   the `install-mcp-*` skills draw from one source?
3. Path predicate richness: prefix-only, or globs / per-repo markers (e.g. a `.symmetria-mcp`
   file in a project root) as well?
4. Lifecycle: register lazily at spawn only, or also offer a "sync all known projects" command?
5. Does writing to `~/.claude.json` from the harness risk racing a concurrently-running agent
   that holds the file open? (Local scope is keyed by path, so collisions are unlikely, but the
   write should still be atomic.)

## Appendix: the concrete miss that motivated this

Goal: make the Trello MCP available in every project under `~/projects/chamba-hq` but nowhere
else. A `claude()` zsh wrapper was added to `~/.dotfiles/.zshrc` that registers Trello at
`local` scope when `$PWD` is under `chamba-hq`. It works from a terminal. It never fired under
the IDE because the IDE spawns `env claude` directly, with no interactive shell to load the
function (see "Why the obvious fix does not work" above). The fallback in use is the
`install-mcp-claude-code` skill, run per project — which works, but must contend with the
credential-expansion trap unless Trello creds are exported at session scope.
