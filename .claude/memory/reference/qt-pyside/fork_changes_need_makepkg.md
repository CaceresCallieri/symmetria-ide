---
name: fork-changes-need-makepkg
description: "qmltermwidget fork changes only reach the IDE launchers after commit + makepkg -sif — the launchers load the pacman package, not the checkout build"
metadata: 
  node_type: memory
  type: reference
  originSessionId: 8d230b5f-4e57-47c9-9633-1c18d5393c7e
---

The `~/.local/bin/symmetria-ide` / `symmetria-ide-stable` launchers set
`SYMMETRIA_IDE_QMLTERMWIDGET_PATH=` **empty**, so `import QMLTermWidget 2.0`
resolves from the pacman package (`/usr/lib/qt6/qml/QMLTermWidget/`), NOT the
checkout at `~/projects/symmetria-qmltermwidget`. Unset vs empty differ:
*unset* (running `python -m symmetria_ide` from the repo) falls back to the
checkout build dir, so smoke tests pass while the launcher path is broken.

**Why:** Adding a new fork Q_PROPERTY and binding it in the IDE's QML makes the
launcher-launched IDE fail at engine load ("cannot assign to non-existent
property") until the system package is updated. Burned 2026-06-12 with
`autoCopySelectedText`.

**How to apply:** after any fork change that the IDE's QML references:
1. commit in the fork (PKGBUILD builds the committed `symmetria` branch only),
2. `cd ~/projects/symmetria-qmltermwidget/packaging && makepkg -sif`,
3. verify via `timeout 12 ~/.local/bin/symmetria-ide` (launcher path, not repo
   `PYTHONPATH=src` runs, which mask the failure).
