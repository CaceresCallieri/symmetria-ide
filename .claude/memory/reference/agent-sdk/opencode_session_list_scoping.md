---
name: opencode-session-list-scoping
description: "`opencode session list --format json` folds worktrees itself but fails OPEN outside a git project — 15 foreign sessions with projectId global"
metadata:
  node_type: memory
  type: reference
---

# `opencode session list --format json` — scope, shape and cost

**SHIPPED 2026-08-15** — code: `agent_threads.py::OpenCodeThreadReader`,
`agent_harness.py::parse_opencode_sessions`,
`app.py::request_opencode_sessions`. This file keeps the MEASUREMENT the code
comments cite, so it can be re-run rather than re-argued
(`.claude/rules/rerun_recorded_measurements.md`).

## Method

Date: 2026-08-15. Host: this Arch/Hyprland workstation. Command, run three
times with nothing changed but the working directory:

```
cd <cwd> && time opencode session list --format json
```

The three working directories were the three that matter for the thread rail:
a linked worktree of this repo, its main checkout, and a directory inside no
git project at all.

## Result — the CLI folds worktrees, and fails OPEN outside a project

| cwd | rows | `projectId` | `directory` |
|---|---|---|---|
| `~/projects/symmetria-ide-wt/<worktree>` | 4 | one id, shared | the MAIN checkout |
| `~/projects/symmetria-ide` (main checkout) | 4, identical | same id | the MAIN checkout |
| `/tmp/sidebar-spike` (no git project) | **15, unrelated** | `"global"` | other repositories |

Two facts, and only the first was previously recorded:

1. **Worktree folding is the CLI's own.** Asked from the worktree or from the
   main checkout, the answer is byte-identical — same 4 rows, same `projectId`,
   `directory` at the main checkout in both. The IDE does no grouping work for
   OpenCode, unlike Claude, where ownership has to be recomputed from each
   transcript's internal `cwd`.
2. **⚠ The failure case is the one that matters, and the older note omitted
   it.** From a cwd inside no git project the same command answers with 15
   sessions from other repositories under `projectId: "global"`. It does not
   fail, it does not return empty — it widens. So the scoping cannot rest on
   the cwd alone.

Both defences shipped, deliberately overlapping: ask at the CANONICAL project
root (`canonical_project_root(resolve_project_root(...))`), and then discard
every row whose `directory` does not canonicalise back onto that root. The
first makes the good answer likely; the second is what keeps a global-scope
answer out of this project's rail even if the cwd rule is ever bypassed.

## Row shape

Exactly `{created, directory, id, projectId, title, updated}`. `updated` and
`created` are epoch MILLISECONDS. Titles are generated and human-readable; an
untitled session carries the placeholder `"New session - <iso timestamp>"`.

There is **no worktree information anywhere in this response** — `directory` is
the main checkout even for a session done inside a linked worktree. An OpenCode
thread older than `agent_thread_store` therefore has nothing to recover, and
resumes in the main checkout with a visible notice. Do not try to infer one.

## Cost

**1.56 s warm** — lower than the 3.8 s recorded earlier for the same command,
and still far too slow for the GUI thread. The asynchronous, publish-second
design in `agent_thread_indexer.py` is unchanged by the smaller number: Claude
finishes its whole file walk in well under a second, so OpenCode is what the
rail would wait on if the readers were serialised into one publication.

Related: [daemon freezes agent env](./daemon_freezes_agent_env.md) for the other
place a harness's own reported identity turned out not to be authoritative.
