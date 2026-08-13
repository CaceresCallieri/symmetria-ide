#!/usr/bin/env bash
# Launch the IDE from THIS worktree with the flat aesthetic, including the
# File Manager half.
#
# The FM's UI is a QML module installed system-wide at
# /usr/lib/qt6/qml/Symmetria/FileManager/UI. The flat-aesthetic changes to it
# live in a sibling worktree and are NOT installed, so without the override
# below you would get the new chrome around the OLD file tree and git badges.
# SYMMETRIA_IDE_FM_QML_PATH prepends the worktree's qml/ to the import path.
#
# This is a DEV instance. It does not touch the stable IDE, and the Hyprland
# rule sends it to workspace 6 silently — it will not steal focus.
#
#   ./scripts/run-flat-aesthetic.sh              # open it and look at it
#   ./scripts/run-flat-aesthetic.sh --shot out.png   # headless screenshot, exits
#
# To see the File Manager standalone with the same palette:
#   cd ~/projects/symmetria-file-manager-wt/flat-aesthetic && ./run.sh
# (that one builds a Qt host binary and DOES open a window on the current
# workspace).

set -euo pipefail

IDE_WORKTREE="$(cd "$(dirname "$(realpath "$0")")/.." && pwd)"
FM_WORKTREE="${SYMMETRIA_FM_WORKTREE:-$HOME/projects/symmetria-file-manager-wt/flat-aesthetic}"

if [[ ! -d "$FM_WORKTREE/qml" ]]; then
  echo "File Manager worktree not found at: $FM_WORKTREE" >&2
  echo "Set SYMMETRIA_FM_WORKTREE to override." >&2
  exit 1
fi

export SYMMETRIA_IDE_FM_QML_PATH="$FM_WORKTREE/qml"
export PYTHONPATH="$IDE_WORKTREE/src"

# Match the production launchers: the EMPTY string is deliberately distinct
# from unset — it means "resolve QMLTermWidget from the installed pacman
# package" rather than from the fork's checkout build dir.
export SYMMETRIA_IDE_QMLTERMWIDGET_PATH=""

if [[ "${1:-}" == "--shot" ]]; then
  export SYMMETRIA_IDE_SCREENSHOT="${2:?usage: --shot <output.png>}"
  export SYMMETRIA_IDE_WARMUP_MS="${SYMMETRIA_IDE_WARMUP_MS:-3000}"
  export SYMMETRIA_IDE_USAGE_POLL=0
fi

cd "$IDE_WORKTREE"
exec python -m symmetria_ide
