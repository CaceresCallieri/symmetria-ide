# File-tree mount optimization — work log + roadmap

Cross-session resume doc for the side-panel file-tree expansion work. The
session that started this captured baseline numbers on bambin (~2200
files / ~480 dirs), shipped option 1 (IDE-side ignored-set short-circuit)
plus several supporting fixes, and identified a menu of follow-ups.

Options 1 and 4 are shipped. Next-up: **option 8 — profile nvim spawn** (now the lowest-hanging remaining target — see bench numbers above).

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

Option 8 in the doc (profile nvim spawn) is now the lowest-hanging
remaining target if we want to push further.

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

### Option 6 — Per-project expanded-state cache

Persist `_expanded` per repo root to `~/.local/state/symmetria-ide/<hash>.json`.
Project re-opens become instant on second visit. JSON-on-disk; minimal
serialization complexity.

Wins big for users who bounce between projects. Doesn't help first-time
mounts.

### Option 7 — Earlier GitController pre-warm

Today the GitController scan kicks off when `set_repo_root` is called
from `displayedRootChanged`. The FileTreeView's first cascade can race
ahead and miss the `ignoredPathSet` data.

Move the scan trigger to the **project capsule arrival** in
`_route_capsule` (or earlier — anchor change?), so the worker is
already mid-scan by the time the FileTreeView mounts.

Low-effort but only ~50-200ms payoff. Worth doing alongside option 4
since they share the same critical path.

### Option 8 — Profile nvim spawn time

Untested. `nvim --embed` with the user's full plugin set might be the
hidden time-to-first-paint floor. If it's ~300ms, optimizing the file
tree below that wouldn't shorten total launch time.

Easy diagnostic: time from `bench/measure_mount.py` process start to
the first FM Logger line. If big, we have a separate optimization
target.

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
2. Re-run baseline benches to make sure the current state matches what
   this doc records (bambin should still settle at ~449ms).
3. Consider option 8 (profile nvim spawn) or option 7 (earlier GitController
   pre-warm) if further startup speedup is needed.
