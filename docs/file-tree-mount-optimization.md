# File-tree mount optimization — work log + roadmap

Cross-session resume doc for the side-panel file-tree expansion work. The
session that started this captured baseline numbers on bambin (~2200
files / ~480 dirs), shipped option 1 (IDE-side ignored-set short-circuit)
plus several supporting fixes, and identified a menu of follow-ups.

Options 1, 4, 6, and 8 are complete. Option 8's outcome was **diagnostic-only**: the
`SYMMETRIA_IDE_TRACE` waterfall shows the post-option-4 cost is dominated by
~400ms of fixed Qt/Python/QML overhead PLUS a 450-700ms post-`Session-started`
mount window. Attempted optimizations (defer AgentPane behind a Loader)
were net-negative on the dominant case (bambin) and reverted. Option 6 ships
a working per-project expanded-state cache — the wall-clock differential vs
lazyExpand is within noise for matched sets, but the **felt UX win is
preserving the user's manually-expanded tree state across sessions**, which
the benchmark cannot synthesize. Next-up candidate is **option 7** (earlier
GitController pre-warm, small standalone win).

## Status — what's shipped

Five commits across three repos cover the option 1 family + observability
fixes:

| Repo | Commit | What |
|------|--------|------|
| `symmetria-ide` | `3a40cd3` | feat: plumb GitController `ignoredPathSet` through FileTreeView. New `_run_ignored_set` worker pass + Qt property + Main.qml binding + dev-mode `SYMMETRIA_IDE_FM_QML_PATH` env override. Also includes the `bench/measure_mount.py` harness. |
| `symmetria-ide` | `6215fe0` | fix: align `_publish` callers with 4-arg signature. Added 6 tests covering `_run_ignored_set` + `ignoredPathSet` contract. Widened `bench/.gitignore` glob; corrected stale "screenshot mode" docstrings. |
| `symmetria-file-manager` | `99167fb` | feat(file-tree): consumer-driven ignored set + deterministic mount-settled emit. New `ignoredPathSet: var` prop with short-circuit; new `tree mount settled` terminal log line; empty-dir pending leak fix via `loadingChanged` + `Qt.callLater`. |
| `symmetria-file-manager` | `74b7c6f` | fix(file-tree): reset `_mountInFlight` on `_refreshAll` + disconnect-before-destroy via new `_destroyModel(m)` helper + gate `ignoredPathSet` fast-path on `respectGitignore`. |
| `~/.hyprdots` | `84fe8b5` + `4ca74f2` | feat(hypr): route `symmetria-ide` to workspace 6 silently. Persists what `dev-workflow.md` documented as a one-shot. |

Plus one critical bug fix bundled into the IDE feat commit: `AppController.start()`
now emits `displayedRootChanged` once at startup. Without that emit, the first
nvim cwd capsule matched `self._cwd` (both initialized from `os.getcwd()`),
short-circuited `_route_capsule`, and `_sync_git_repo_root` never ran — leaving
both the Active Changes panel AND the gitignore short-circuit silently broken
whenever the IDE was launched in its own cwd. That's why the user's first
relaunch with the option 1 wiring showed empty panels.

## Bench numbers

`bench/measure_mount.py` polls the FM Logger for `tree mount settled: N rows
visible` and computes wall-clock from "Session started" through the LAST
emission. Run with `SYMMETRIA_IDE_FM_QML_PATH=~/projects/symmetria-file-manager/qml`
to pick up source-tree FM changes without reinstalling.

| Repo | Pre-option 1 | Post-option 1 | Post-option 4 | Reduction (full) |
|------|-------------|---------------|---------------|------------------|
| `~/work/sales/bambin` (~2200 files, ~480 dirs) | **3994ms** | **2303ms** | **449ms** | 89% (8.9× speedup) |
| `~/projects/symmetria-ide` (small) | <1ms* | 548ms | 442ms | — |
| `~/.dotfiles` (small) | <1ms* | 449ms | 479ms | flat |

\* Baseline for the small repos was measuring the Active Changes panel
(filtered, settles instantly) before the GitController-wake fix landed.
Post-fix, both panels mount and the bench measures the slower of the two —
hence the "regression" that's actually a more honest number.

The 449ms remaining on bambin is dominated by **nvim spawn + first
git scan + Logger 500ms flush window** — file-tree work is no longer
the bottleneck. The cascade now expands exactly 3 directories to fill
the side panel viewport (49 visible rows on a 1280x720 launch at
compactScale 0.8) instead of cap-tripping at 100 directories.

### Option 8 trace waterfall (added 2026-05-23)

Run with `--trace` to capture `SYMMETRIA_IDE_TRACE=1`-gated phase
markers from the IDE's stderr. Median across 15 runs (5 each on
bambin / symmetria-ide / dotfiles):

| Phase | Cumulative ms | Δ from prior |
|-------|---------------:|-------------:|
| `imports_basic_done` | 5 | — |
| `app_module_imported` (PySide6 + pynvim + all submodules) | 148 | +143 |
| `qgui_created` | 165 | +17 |
| `engine_ctx_ready` (Python-side `_build_engine` setup) | 170 | +5 |
| `engine_loaded` (Main.qml + all eager imports parsed + instantiated) | 270 | +100 |
| `backend_started` (nvim subprocess up via pynvim worker) | 280 | +10 |
| `terminal_started` (PTY + pyte ready) | 292 | +12 |
| `start_done` | 292 | +0 |
| `exec_entered` (Qt event loop running) | 310 | +18 |
| `first_capsule` (first nvim capsule seen in Python) | 316 | +6 |
| `git_ignored_published` (GitController worker emits ignored set) | 325 | +9 |
| FM Logger "Session started" emit | ~300-310 (overlaps QML eval) | — |
| FM Logger "tree mount settled" emit (bambin) | ~896 | +571 (= `tree_mount_ms`) |

**Findings:**

- The 148ms Python import cost is dominated by PySide6 itself
  (QtCore/QtGui/QtQml). Top-of-`app.py` modules are eager because the
  `@QmlElement`-decorated classes (NvimView, TerminalView, SessionModel,
  CmdlineState, …) must be class-loaded before `_register_qml_types`
  fires, otherwise QML can't resolve `Symmetria.Ide 1.0` types. Lazy-
  importing those breaks the registration contract.
- The 100ms `engine_loaded` slice is `engine.load(Main.qml)` — actual
  QML parse + instantiate of every eagerly-referenced type. Qt's QML
  cache (`~/.cache/Symmetria/Symmetria IDE/qmlcache/*.qmlc`) is already
  hot on subsequent launches, so this is the *cached* path.
- The 100ms `controller.start()` → `exec_entered` slice is dominated
  by `gc.collect()` + `gc.freeze()` (gotcha #10 partner). These are
  load-bearing for the 3.14 SEGV mitigation; do not delete the
  `collect` "for speed".
- The 4-9ms `git_ignored_published` slice means the GitController
  worker scan completes ~570ms *before* tree_mount settles. Option 7
  (earlier GitController pre-warm) would help only if the FM cascade
  ever races the worker — currently it doesn't.

**Attempted but reverted:** wrapping AgentPane in a Loader
(`active: controller.agentVisible || item !== null`). Saved
~12-25ms in `engine_loaded` but regressed bambin's `tree_mount_ms`
by 60-120ms (smaller repos were neutral). Hypothesis: removing
AgentPane from the eager-evaluation graph reshuffles the QML
engine's first-frame scheduling in a way that contends with the
FM's incremental row-fan-out. The `Main.qml` regression note
documents the experiment so a future agent doesn't repeat it
without re-bench on bambin.

**What's tractable next:**

- Option 6 (per-project expanded-state cache): persist `_expanded`
  per repo root, project re-opens become instant on second visit.
- Option 7 (earlier GitController pre-warm): minor (~50-200ms) win
  alongside any other change touching `_route_capsule`.

The `--trace` infrastructure is permanent — re-run with
`SYMMETRIA_IDE_TRACE=1` (or via `bench/measure_mount.py --trace`) any
time the launch waterfall changes.

## Status — option 4 SHIPPED

Viewport-driven (lazy) auto-expand landed via the `lazyExpand: bool` prop
on `FileTreeView.qml` + IDE-side flip in `Main.qml`. The new mount cycle:
expand root → check if rendered rows fill viewport + buffer → if not,
expand the first un-expanded dir in row order → re-check. Scroll +
viewport-resize + compactScale-change re-arm the same cycle, so the
tree extends on demand.

While benching this work, surfaced a **pre-existing latent bug** in
commit `74b7c6f` (last session's fix-commit): `m._scheduleOnChange = fn`
fails silently under QML's strict-property mode (`pragma
ComponentBehavior: Bound`) — assignments to non-declared properties on
C++ QObjects are no-ops with a Qt-log warning. The disconnect machinery
in `_destroyModel` never had a handler to find. Fixed by replacing the
dynamic property assignment with a path-keyed `_modelHandlers` JS map.
Did NOT block real user flow because the **installed** FM
(`/usr/lib/qt6/qml/...`) predated `74b7c6f` and never had the broken
line; the bench's source-tree FM exposed it.

## Pending options — ranked

#### Why it beat options 2 + 3 (shipped — kept for posterity)

- Option 2 (BFS-level gitignore batching) is now mostly a standalone-FM
  win — the IDE already short-circuits via `ignoredPathSet`.
- Option 3 (shared `FileSystemModel` cache across both side-panel trees)
  is real but smaller — halves inotify usage and dedupes some scans, but
  the dominant cost is the number of `_expand` calls, not their per-call
  overhead.
- Option 4 attacks the dominant cost directly: fewer `_expand` calls.

#### Design as shipped

The FM's `FileTreeView` has two relevant props today:
- `initialExpandDepth: -1` (mount-only recursive auto-expand)
- `maxExpandDepth: 8`

Replace recursive auto-expand with a **scroll-driven incremental expand**:

1. At mount, expand only the root directory.
2. Once `view.contentHeight > view.height` (we've filled the viewport),
   stop expanding. We're done — the rest will come from user actions or
   scroll.
3. Otherwise, expand the next un-expanded directory in row-order (first
   subdir of the root that's not yet expanded).
4. Repeat (2-3) until viewport is full OR all dirs at depth-1 are
   expanded.
5. **On scroll**: when the user scrolls and a not-yet-expanded directory
   becomes visible in the viewport, expand it on demand.

The "next un-expanded directory" walk needs to be careful — directory
entries always sort first (FM convention from `compareEntries` in
`filesystemmodel.cpp:702`), so a depth-first walk of subdirs is the
right strategy. The viewport-fill condition is simply
`view.contentHeight >= view.height + N` where N is a small buffer for
smooth scrolling (8-16 rows).

#### Files to touch (option 4)

Both files are in `~/projects/symmetria-file-manager` (the FM repo):

1. `qml/Symmetria/FileManager/UI/modules/filemanager/FileTreeView.qml`
   - Add a new prop `lazyExpand: bool` (default false — preserves existing
     callers). When `true`, the existing `initialExpandDepth` cascade is
     bypassed in favor of the viewport-driven approach.
   - Add internal helper `_expandUntilViewportFilled()` that walks `_rows`,
     finds the first non-expanded directory, calls `_expand(path)`, and
     re-checks after the model settles (via the existing `Qt.callLater`
     mechanism).
   - Add a `Connections` block on `view` listening to `onContentHeightChanged`
     (or a scroll position binding) to trigger expand-on-scroll.
   - The existing `_autoExpandActive` flag stays — `_mountInFlight` semantics
     unchanged.

2. `qml/Main.qml` in `symmetria-ide`
   - Change the main `FileTreeView` from `initialExpandDepth: -1` to
     `lazyExpand: true` (drop the `initialExpandDepth` line entirely).
   - The Active Changes panel (`GitStatusPanel.qml`) likely stays
     `initialExpandDepth: -1` since its `pathFilter` already constrains
     the tree to the small changeset — eager is fine there.

#### Tradeoffs / gotchas

- The "viewport filled" check needs the row delegate's actual height,
  not the prop's compactScale-adjusted estimate. Use `view.contentHeight`
  vs `view.height` directly.
- Density change (`compactScale` mutation post-mount) might change how
  many rows fit — the lazy expand should re-trigger when this happens.
- `_autoExpandFanoutCap: 200`, `_autoExpandModelCeiling: 100`,
  `_autoExpandNodeCeiling: 10000` should still apply as safety nets in
  case viewport-fill triggers a runaway loop (e.g., directories with
  one-line names that don't fill the viewport even after many expansions).
- The `pathFilter`-narrowed cascade in `GitStatusPanel.qml` already
  works correctly — leave that as `initialExpandDepth: -1`.

### Option 2 — BFS-level gitignore batching

For the standalone FM (no IDE consumer). When `ignoredPathSet` is null
(no IDE), the per-dir `gitignoreSvc.filter` queue dominates. Collect all
candidates from a fan-out round into one combined stdin to
`git check-ignore --stdin`. Wins one shell-spawn per BFS level instead
of one per directory.

Files: `qml/Symmetria/FileManager/UI/services/Gitignore.qml` (the
runner), plus possibly the `_autoExpandChildrenOf` fan-out in
`FileTreeView.qml` to drive batch boundaries.

Skip if option 4 lands first — lazy expand removes most of the
gitignore queue pressure regardless of batching.

### Option 3 — Shared FileSystemModel cache

Two `FileTreeView` instances mount over the same root (main tree +
Active Changes panel) — each spawns its own `FileSystemModel` per dir.
Halve the dir scans and inotify watch slots by sharing via a process-
wide cache keyed on `(path, showHidden, sortBy)`.

Largest architectural impact of the remaining options; biggest
lifetime-management risk (refcounting). Defer unless inotify watcher
exhaustion becomes a real issue on huge trees.

### Option 5 — Inotify watcher consolidation

Replace per-dir `QFileSystemWatcher` with a single recursive inotify
watch rooted at the project. C++ work in `FileSystemModel`; not
trivial because `QFileSystemWatcher` doesn't expose recursive mode.
Could use direct `inotify(7)` syscalls + a Python overlay, or a third-
party Qt addon.

Real benefit only when hitting `fs.inotify.max_user_watches` (8192
default). Bambin doesn't hit this; large monorepos might.

### Option 6 — Per-project expanded-state cache *(SHIPPED 2026-05-25)*

Persist the set of expanded directories per repo root to
`$XDG_STATE_HOME/symmetria-ide/projects/<hash>.json`. Cache is consulted
synchronously at AppController `__init__`-time (BEFORE QML loads, so the
FM's `restoreExpandedPaths` prop holds the right list on first
`onRootPathChanged`), then refreshed on every subsequent
`displayedRootChanged` via `_sync_expanded_paths_cache`. Persistence
fires on every user-driven expand/collapse via
`FileTreeView.expandedStateChanged → controller.saveExpandedPaths`.

**Files shipped:**

| Repo | File | What |
|------|------|------|
| `symmetria-ide` | `src/symmetria_ide/tree_state_cache.py` | New module: `load_expanded`, `save_expanded`, atomic-write via `os.replace`, stat-prune on load, schema-version gated. |
| `symmetria-ide` | `src/symmetria_ide/app.py` | New `expandedPathsCache` property + `expandedPathsCacheChanged` signal + `_sync_expanded_paths_cache` slot (wired BEFORE `_sync_git_repo_root` in connect order) + `saveExpandedPaths(list)` slot + pre-populate call at end of `__init__`. |
| `symmetria-ide` | `qml/Main.qml` | Main FileTreeView gets `restoreExpandedPaths: controller.expandedPathsCache` + `onExpandedStateChanged: controller.saveExpandedPaths(paths)`. |
| `symmetria-ide` | `tests/test_tree_state_cache.py` | 15 tests: missing→empty, round-trip, dup-coalesce, stale-path prune, corrupted-JSON, schema-version safety, atomic-write tmp-cleanup, XDG path resolution, empty-input writes a file. |
| `symmetria-file-manager` | `qml/Symmetria/FileManager/UI/modules/filemanager/FileTreeView.qml` | New `restoreExpandedPaths: var` prop (priority over `lazyExpand`/`initialExpandDepth`), `expandedStateChanged(var paths)` signal, internal `_restorePending`/`_restoreActive` state, `_advanceRestoreFor(expandedPath)` driver chains restore through the existing `_expand` finish callback. |

**Restore semantics.** When `restoreExpandedPaths` is a non-empty list:
1. `onRootPathChanged` skips the lazyExpand/BFS cascade.
2. Paths filtered to those under `rootPath` and sorted shortest-first.
3. `_expand(rootPath)` runs first; its finish callback dispatches
   `_advanceRestoreFor(rootPath)` which scans the queue for any
   direct children and expands them.
4. Each child's finish callback recurses — natural depth-first
   replay driven by the existing async machinery, no new chain
   layer. `_generation` invalidates in-flight steps if rootPath
   changes mid-restore, same as model creation.
5. `_restoreActive` clears when the queue drains; the mount-settled
   emit fires on the next `pendingEmpty` cycle, log line reads
   `tree mount settled: N rows visible (lazy: 0 dirs)`.
6. `expandedStateChanged` is suppressed during restore to avoid
   churning the consumer's disk-write path while replaying the
   already-saved set.

**Bench results (symmetria-ide, 5 runs each):**

| Scenario | tree_mount_ms (median) | Notes |
|----------|-----------------------:|-------|
| No cache (lazyExpand) | 608ms | 42 rows, 5 dirs |
| Warm cache, same set as lazy would produce | 615ms | 42 rows, restore replaces lazyExpand |
| Warm cache, deep set (14 dirs) | 949ms | 117 rows, restore replays user's exploration |

The flat wall-clock numbers (608/615) confirm: when the cache contains
exactly the set lazyExpand would produce, the two paths are
equivalent-cost. The deep-set timing (949ms) is purely additive — more
work because more dirs are expanded. That additional cost IS the win:
it's the tree shape the user actually wants back, not the viewport-fill
default.

**Felt-UX value (not bench-visible).** The benchmark synthesizes a cold
mount on a clean repo. The real user workflow is: open project →
explore (expand 5-15 dirs across the tree) → close IDE → re-open
project the next day. Without option 6, day 2 starts with lazyExpand's
default viewport-fill (typically only 5-6 dirs of root-level children).
With option 6, day 2 restores exactly the set the user built up the
day before — same scroll-to-find behavior, same mental model.

**Gotchas internalized (added to the section at the bottom).**

- QML `Array.isArray(qVariantList)` returns false in Qt 6.11 — use
  duck-typed `cache != null && cache.length > 0` (gotcha #11).
- Cache must be pre-populated in `AppController.__init__` (NOT only
  via the `displayedRootChanged` slot) because the FM mounts at
  `engine.load(Main.qml)`, BEFORE `start()`'s synthetic
  `displayedRootChanged` emit (gotcha #12).

**v2 follow-ups (not in v1).**

- Multi-window race on the cache file: today "last write wins"; if
  two IDE instances open the same repo, the second's first save
  clobbers the first's saved state. Atomic-write protects against
  corruption but not lost updates. Fix would be a `fcntl.flock`
  on write — overkill for v1's expected usage.
- Restore-path display order: the chain is depth-first by
  `_advanceRestoreFor`, but row-visibility comes through
  `_rebuildRows` which is breadth-first by `_rows` traversal. A
  user watching a slow restore would see rows pop in a non-obvious
  order. Not visible at usual speeds.

### Option 7 — Earlier GitController pre-warm

Today the GitController scan kicks off when `set_repo_root` is called
from `displayedRootChanged`. The FileTreeView's first cascade can race
ahead and miss the `ignoredPathSet` data.

Move the scan trigger to the **project capsule arrival** in
`_route_capsule` (or earlier — anchor change?), so the worker is
already mid-scan by the time the FileTreeView mounts.

Low-effort but only ~50-200ms payoff. Worth doing alongside option 4
since they share the same critical path.

### Option 8 — Profile nvim spawn time *(SHIPPED as diagnostic-only — see Status section above)*

Done as a `SYMMETRIA_IDE_TRACE`-gated phase tracer
(`src/symmetria_ide/trace.py`) wired through `__main__.py`, `app.py`
(`run()`, `AppController.start()`, `_build_engine`, `_route_capsule`),
and `git_controller.py` (`_publish`). `bench/measure_mount.py` grew a
`--trace` flag that captures stderr to a tempfile and parses the trace
lines into each run's metrics dict alongside the existing
`tree_mount_ms` measurement.

**Outcome:** there is no big-bang fix here. The 400ms pre-`Session-started`
window is mostly fixed Qt/Python/PySide6 overhead. `nvim --embed`
itself is fast — `start_begin → backend_started` is only ~10ms because
pynvim spawns the subprocess and returns immediately; the user's
plugin load happens in the background while QML is mid-instantiation.
The waterfall table in the Status section above is the authoritative
breakdown. Tried-and-reverted notes for what didn't work are captured
in `qml/Main.qml` at the AgentPane regression-note comment.

If a future agent wants to push pre-`Session-started` further, the
remaining options would require:
- Native QML compilation via `qmlcachegen --resource` instead of
  Qt's runtime cache (might shave engine_loaded but adds build step).
- Splitting `app.py` so QML-registered classes are tighter (their
  imports already dominate `app_module_imported`).
- Skipping `gc.collect()` before `gc.freeze()` (~15ms) — DANGEROUS,
  see gotcha #10 partner pattern.

None are worth doing without a specific user-visible launch
complaint pointing at them.

## Gotchas learned this session

These are non-obvious and will burn future readers. Worth scanning
before touching FileTreeView or GitController:

1. **`displayedRootChanged` must fire at start()** — `_cwd` is initialised
   from `os.getcwd()`; the first nvim cwd capsule typically matches, so
   the equality gate in `_route_capsule` silences the signal. Fix: synthetic
   emit in `AppController.start()`. See `app.py:1890` (the comment is
   load-bearing — a future "cleanup" that removes it will silently break
   GitController on launch).

2. **`loadingChanged` fires BEFORE `applyChanges` in `FileSystemModel`** —
   handlers connected to it see empty `m.entries` for non-empty dirs.
   Fixed via `Qt.callLater(onChange)` in FileTreeView's `_expand`. Don't
   refactor this to a synchronous handler without re-introducing the
   empty-cascade bug.

3. **Empty dirs never fire `entriesChanged`** — the C++ `applyChanges`
   only emits when `addedEntries` OR `removedPaths` is non-empty
   (`filesystemmodel.cpp:684-699`). Fixed by also connecting to
   `loadingChanged` (one signal always fires per scan cycle).

4. **`Qt.callLater` callbacks can outlive their model object** — added
   `_destroyModel(m)` helper that disconnects signals before destroy.
   Every destroy site must go through it, or use-after-destroy on a
   C++ pointer.

5. **`GitController.ignoredPathSet` returns `None`, not `{}`, when no scan
   has happened** — load-bearing for the FM's fallback gate. A truthy-
   empty dict would let the FM treat nothing as ignored and over-expand
   `.venv` / `node_modules` etc. before the first scan completes.

6. **`set_repo_root` clears `_ignored_set = {}` synchronously** —
   needed so the FM's fallback gate also fires on project switches, not
   just initial launch. Tests cover both empty-then-populated and
   populated-then-cleared transitions.

7. **The bench polls log file rather than using screenshot mode** —
   `SYMMETRIA_IDE_SCREENSHOT` triggers `app.quit()` at a fixed warmup
   deadline that races Logger's async `ShellRunner` flush, dropping the
   last few log lines. Polling + explicit settle window guarantees the
   terminal "tree mount settled" line hits disk.

8. **The FM is installed at `/usr/lib/qt6/qml/Symmetria/...` and that
   wins over `QML2_IMPORT_PATH` / `QML_IMPORT_PATH` env vars** — must
   use `engine.setImportPathList([dev_path, *engine.importPathList()])`
   to prepend (not `addImportPath` which appends and loses). See
   `SYMMETRIA_IDE_FM_QML_PATH` handling in `app.py:_build_engine`.

9. **When sealing repos with multiple unrelated diffs in one file**:
   the only safe path is to capture the pre-edit state explicitly, reset
   the file to HEAD, re-apply your edit alone, commit, then restore the
   pre-existing diffs to the working tree from the saved copy. The
   commit agent will otherwise stage the whole file's working-tree diff
   including pre-existing unrelated work.

10. **QML typed parameters do NOT support default values in Qt 6.11** —
    `function _destroyModel(m: var, path: string = ""): void` parses
    green in tests but fails at engine load with `Type annotations are
    not supported (yet)` at the column of the `= ""`. The error cascades
    to "Type FileTreeView unavailable" → "Type FmUi.FileManager
    unavailable" → "failed to load Main.qml". A 2026-05-23 review
    landed this regression in FM commit `fc509f2`; the live IDE worked
    only because the *installed* FM at `/usr/lib/qt6/qml/...` still had
    the older signature. Use `function _destroyModel(m: var, path: string)`
    (typed but no default) and let `if (path && path !== "")` guard the
    `undefined`-passed call sites. The regression note in
    `FileTreeView.qml` records this in detail.

11. **`Array.isArray(qVariantList)` returns false in QML (Qt 6.11)** —
    PySide6 marshals a Python `list` (e.g. an `@Property(list, ...)`)
    into QML as a `QVariantList`, which is array-LIKE (has `length`,
    integer indexing, iteration) but does NOT satisfy
    `Array.isArray()`. A check of the shape
    `if (Array.isArray(prop) && prop.length > 0)` silently fails
    every time on QVariantList-backed props, regardless of what the
    Python side actually contains. The duck-typed form
    `if (prop != null && prop.length > 0)` works for both true JS
    Arrays and QVariantList. Burned us on option 6's
    `restoreExpandedPaths` initial implementation — the property
    was correctly populated Python-side, the QML cast was fine, but
    the entry gate rejected every restore. Diagnosed by adding a
    log line inside the FileTreeView and seeing the rejected
    candidate. Code that combines a JS Array literal AND a
    QVariantList from Python through the same prop must use the
    duck-typed gate.

12. **AppController must pre-populate state-loaded-on-displayedRootChanged
    in `__init__`** — anything keyed on `displayedRootChanged` (like
    the option-6 expanded-paths cache) needs an initial load BEFORE
    QML instantiates the consumer component, OR the consumer reads
    a stale initial value once and the later signal-driven update
    doesn't retrigger the consumer's own onPropChanged handler if
    its parent state (`rootPath`) hasn't visibly changed. The
    fixed order is: (1) connect the slot to `displayedRootChanged`;
    (2) call the slot manually once. Then QML's first read sees
    the loaded state, and subsequent project switches go through
    the normal signal path. Same class of bug as gotcha #1
    (`displayedRootChanged` not firing at start because `_cwd`
    matches `os.getcwd()`) but at a different layer — even with
    the synthetic emit in `start()`, the timing is wrong if QML
    has already mounted at `engine.load()` time.

13. **Bambin's `tree_mount_ms` is noisy: ±100ms run-to-run** — the bench
    measures from `Session started` (Logger.qml.onCompleted) to last
    `tree mount settled`. Variance comes from disk-cache state, fsync
    cadence, system load, and Qt's scene-graph scheduler. Treat any
    change <50ms as below the noise floor on bambin. Smaller repos
    (symmetria-ide, dotfiles) have ~30ms variance. Always bench 5+ runs
    per repo and use the trimmed median (mounts[1:-1]).

## Bench harness usage

```
SYMMETRIA_IDE_FM_QML_PATH=~/projects/symmetria-file-manager/qml \
PYTHONPATH=src \
python bench/measure_mount.py \
    --repo ~/work/sales/bambin \
    --repo ~/projects/symmetria-ide \
    --repo ~/.dotfiles \
    --runs 5 \
    --timeout 30 \
    --label <description> \
    --out bench/results-<description>.json
```

Per-run JSON output is gitignored (`bench/.gitignore` covers
`results*.json`). The harness expects exactly one `tree mount settled`
emit per run unless `--expected-trees 2` is passed.

## Resume checklist for next session

1. Read this file end-to-end (especially the gotchas).
2. Re-run baseline benches to confirm current state. Expect bambin
   `tree_mount_ms` to settle around 500-700ms (high variance, see
   gotcha #13). Use `--trace` to also capture the pre-`Session-started`
   waterfall.
3. Options 1, 4, 6, and 8 are done. Pick from the remaining:
   - **Option 7** (earlier GitController pre-warm) — small (~50-200ms)
     win, low effort.
   - **Option 2** (BFS-level gitignore batching) — standalone-FM only;
     the IDE already short-circuits via `ignoredPathSet`.
   - **Option 3** (shared FileSystemModel cache) — bigger refactor,
     halves inotify usage. Defer unless watcher exhaustion is real.
