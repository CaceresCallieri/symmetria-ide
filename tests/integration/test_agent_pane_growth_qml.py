"""Out-of-process Qt Quick proof for append-only pane delegate growth."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE = REPO_ROOT / "tests" / "qml_harness" / "pane_growth_probe.py"


def test_growing_registry_from_three_to_nine_destroys_no_live_delegate() -> None:
    """Guard: the measured lifetime invariant is exercised by real Qt Quick."""
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    completed = subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, (
        f"pane-growth probe exited {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    assert json.loads(completed.stdout) == {
        "before": 3,
        "after": 9,
        "destroyed_initial": 0,
    }
