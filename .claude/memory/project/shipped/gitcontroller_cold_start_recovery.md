---
name: gitcontroller-cold-start-recovery
description: "GitController self-heals when a dir opened pre-`git init` later becomes a repo (sentinel watch + backed-off re-resolve, no nvim)"
metadata: 
  node_type: memory
  type: project
  originSessionId: 6e18d2b3-fa99-4df3-ae3c-7e2a050321cf
---

**Shipped 2026-06-26.** Pointer: `src/symmetria_ide/git_controller.py` —
`_arm_repo_sentinel` / `_on_watched_dir_changed` / `_on_sentinel_backstop`
(+ `_SENTINEL_BACKSTOP_*`). Tests: `tests/test_git_controller.py`
(`test_status_recovers_after_git_init_without_any_capsule` + the sentinel/
backstop unit tests).

**Root cause (non-obvious, don't re-derive):** if a project directory is
opened in the IDE *before* it is a git repo (canonical case: a brand-new
project where `git init` runs minutes/hours after the IDE window opened),
`GitController`'s first `_do_scan` resolves "not a repo" → `_refresh_watcher_for_root("")`
tore down BOTH the worktree watcher AND short-circuited before arming the
`.git` trigger-file watcher. The only re-resolve trigger was
`displayedRootChanged` → `set_repo_root`, which never fires for a directory
that merely *gains* a `.git` (same path string, idempotent early-return). So
the status panel froze "clean" for the life of the instance while the repo
filled with commits. The status bar's branch still showed because nvim
re-detects git independently on BufEnter/DirChanged — a separate path. This
was found live via `/proc/<pid>` inotify-watch inspection: the broken
instance had ZERO watches on the repo subtree or `.git` files, every healthy
sibling had hundreds.

**Why the fix is self-contained (load-bearing for the nvim-deprecation goal):**
recovery must NOT depend on an nvim capsule (the `branch`-capsule option was
rejected for exactly this reason). The directory SENTINEL — a single
QFileSystemWatcher dir watch on the asked root while not-a-repo — fires
`directoryChanged` when `.git` appears, waking a re-scan that resolves and
arms the real watchers. The backed-off re-resolve backstop (1s→…→60s cap,
stops on resolve) covers the two cases the watch misses: arm failure and the
`git init` partial-init race (`.git` exists but `rev-parse` momentarily
fails, and later writes land *inside* `.git`, not as children of the watched
root). Only the filesystem is involved — survives nvim removal untouched.

Related: a secondary latent gap surfaced in the same investigation —
`WorktreeWatcher.set_root`'s `try/except OSError` around `observer.start()`
can't catch a recursive-watch arming failure, because watchdog adds the
walked watches on its emitter thread (`on_thread_start` → `os.walk` +
`inotify_add_watch`), not on the caller. A mid-walk `ENOENT` (agent churning
`node_modules` during `pnpm install`) silently kills the watch with no
degradation warning. NOT yet fixed — separate hardening if it recurs. See
[Startup performance](../../reference/qt-pyside/startup_perf.md) for the
`/proc` inspection technique used to diagnose both, and
[processEvents shared-app SEGV](../../reference/qt-pyside/processevents_shared_app_segv.md)
for the test hazard hit while landing this fix.
