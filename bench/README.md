# Side-panel file-tree mount benchmark

`measure_mount.py` launches the IDE in screenshot mode against one or more
repos and records the wall-clock between "QML engine up"
(Logger session-start emission) and "both side-panel trees settled"
(last `FileTreeView auto-expand complete` emission). Output lands as JSON
under `bench/results-<label>.json` plus a printed summary.

Used to gate the file-tree expansion optimisations (see CLAUDE.md and the
[Symmetria FM `FileTreeView.qml`](../../symmetria-file-manager/qml/Symmetria/FileManager/UI/modules/filemanager/FileTreeView.qml)).

## One-off

```
PYTHONPATH=src python bench/measure_mount.py \
    --repo ~/work/sales/bambin \
    --repo ~/projects/symmetria-ide \
    --repo ~/.dotfiles \
    --runs 5 --label baseline \
    --out bench/results-baseline.json
```

Close any standalone Symmetria-FM instances first — they share the log
file at `~/.local/share/symmetria/logs/filemanager.log` and would pollute
the parse.

## Reading results

* `median_mount_ms` — central tendency for that repo over `runs` runs
  (we drop fastest + slowest before taking the median).
* `runs[].tree_mount_ms` — per-run delta in ms.
* `runs[].auto_expand_count` — how many trees emitted `auto-expand complete`.
  Should be 2 normally (Active Changes + main); 1 if the working tree was
  clean (Active Changes auto-hides).
* `runs[].filetree_lines` — total `FileTreeView` Logger lines; proxy for
  how chatty the expansion path was.
