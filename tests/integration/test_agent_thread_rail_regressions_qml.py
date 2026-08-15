"""Out-of-process Qt Quick regressions for repaired thread-rail edges."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE = REPO_ROOT / "tests" / "qml_harness" / "thread_rail_regression_probe.py"


def test_dead_rows_and_repeated_close_keep_their_ui_contract() -> None:
    """Dead rows stay inert; closing a live row returns focus to the rail."""
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
        f"thread-rail probe exited {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    assert json.loads(completed.stdout) == {
        "live_title": "live title from pool",
        "live_worktree": "live-worktree-from-pool",
        "dead_title": "durable dead title",
        "dead_worktree": "durable-dead-worktree",
        "dead_stt_target": False,
        "dead_browser_owned": False,
        "dead_browser_attention": False,
        "dead_coord_attention": False,
        "close_calls": [1],
        "rail_focus_after_close": True,
        "sink_focus_after_close": False,
    }
