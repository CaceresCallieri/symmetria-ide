"""Benchmark: time-to-interactive for the IDE's side-panel file tree.

Launches the IDE in screenshot mode against a repo, captures the Logger
timestamps emitted by the QML side, and reports the delta between
"Logger Session started" (QML engine up) and the LAST "FileTreeView
auto-expand complete" line (both side-panel trees settled).

Usage:
    PYTHONPATH=src python bench/measure_mount.py \\
        --repo ~/work/sales/bambin \\
        --repo ~/projects/symmetria-ide \\
        --repo ~/.dotfiles \\
        --runs 5 --label baseline --out bench/results-baseline.json

Methodology notes:
  * The log file at ~/.local/share/symmetria/logs/filemanager.log is
    truncated before every run so we only read this run's events. If you
    have the standalone FM open in parallel its events would mix in
    — close it first.
  * Logger.qml flushes on a 500ms timer, so we keep the screenshot
    warmup ≥ that to be safe.
  * For >=3 runs we drop the fastest + slowest before computing the
    median to absorb cold-cache + GC stutter outliers.
  * The IDE mounts TWO FileTreeViews (Active Changes + main). We take
    the LAST "auto-expand complete" emission per run — that's the
    moment the whole side panel is interactive.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_FILE = Path.home() / ".local/share/symmetria/logs/filemanager.log"
IDE_ROOT = Path(__file__).resolve().parent.parent

# Matches Logger.qml's wire format:
#   [2024-05-22T18:42:13.456Z] [INFO] [Component] message
LOG_LINE_RE = re.compile(
    r"\[(?P<ts>[0-9T:.+\-Z]+)\]\s+\[(?P<lvl>[A-Z]+)\]\s+\[(?P<comp>[^\]]+)\]\s*(?P<msg>.*)"
)
SESSION_STARTED = "Session started"
# `tree mount settled` is FileTreeView's terminal emit per mount — fires once
# regardless of whether the BFS finished naturally, hit the model ceiling, or
# was disabled (initialExpandDepth: 0).
TREE_MOUNT_SETTLED = re.compile(r"tree mount settled:\s*(\d+)\s+rows visible")


def parse_iso(ts: str) -> float:
    """Parse Logger's JS-style ISO timestamp to a POSIX epoch float."""
    # JS `Date().toISOString()` always ends in 'Z'; Python's fromisoformat
    # accepts that on 3.11+. We force UTC explicitly to avoid local-tz drift.
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).timestamp()


def parse_log(text: str) -> dict[str, Any]:
    """Extract bench-relevant metrics from a Logger log dump.

    Returns a dict with:
      - session_started_ts: epoch of Logger init
      - last_auto_expand_ts: epoch of the LAST auto-expand complete emit
      - last_rows: rows visible reported at that emit
      - auto_expand_count: how many FileTreeView mounts settled
      - filetree_lines: total Logger lines from FileTreeView (proxy for
        expansion activity; useful for option 1/2 attribution)
      - shellrunner_lines: total ShellRunner spawn lines (proxy for
        per-dir gitignore subprocess count once we add that emission)
    """
    session_started_ts: float | None = None
    last_auto_expand_ts: float | None = None
    last_rows: int | None = None
    auto_expand_count = 0
    filetree_lines = 0
    shellrunner_lines = 0

    for line in text.splitlines():
        m = LOG_LINE_RE.match(line)
        if not m:
            continue
        comp = m.group("comp")
        msg = m.group("msg")
        ts_raw = m.group("ts")

        if comp == "Logger" and SESSION_STARTED in msg and session_started_ts is None:
            session_started_ts = parse_iso(ts_raw)
        if comp == "FileTreeView":
            filetree_lines += 1
            mm = TREE_MOUNT_SETTLED.search(msg)
            if mm:
                last_auto_expand_ts = parse_iso(ts_raw)
                last_rows = int(mm.group(1))
                auto_expand_count += 1
        if "ShellRunner" in comp:
            shellrunner_lines += 1

    return {
        "session_started_ts": session_started_ts,
        "last_auto_expand_ts": last_auto_expand_ts,
        "last_rows": last_rows,
        "auto_expand_count": auto_expand_count,
        "filetree_lines": filetree_lines,
        "shellrunner_lines": shellrunner_lines,
    }


def _count_settled(text: str) -> int:
    return sum(1 for line in text.splitlines() if "tree mount settled" in line)


def run_once(
    repo: Path,
    *,
    expected_trees: int,
    poll_interval_s: float,
    settle_after_match_s: float,
    timeout: int,
) -> dict[str, Any]:
    """Launch the IDE, poll the log until N "tree mount settled" lines land,
    then SIGTERM. Returns parsed metrics.

    We poll the log file rather than use SYMMETRIA_IDE_SCREENSHOT because that
    path calls app.quit() at a fixed warmup deadline, which races Logger's
    async ShellRunner flush — the last few log lines get dropped on exit and
    the bench saw stale "pending=3" without ever seeing pending=0. Polling +
    explicit settle window guarantees the terminal lines hit disk.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(IDE_ROOT / "src")
    # Do NOT set SYMMETRIA_IDE_SCREENSHOT — we manage shutdown ourselves.

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text("")

    t0 = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-m", "symmetria_ide"],
        cwd=str(repo),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = t0 + timeout
    matched_at: float | None = None
    while True:
        if proc.poll() is not None:
            break
        text = LOG_FILE.read_text() if LOG_FILE.exists() else ""
        if _count_settled(text) >= expected_trees:
            matched_at = time.monotonic()
            break
        if time.monotonic() > deadline:
            break
        time.sleep(poll_interval_s)

    # Give Logger's 500ms timer one more cycle to flush.
    if matched_at is not None:
        time.sleep(settle_after_match_s)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)

    # One more flush window after process death.
    time.sleep(0.6)
    wall_s = time.monotonic() - t0

    log_text = LOG_FILE.read_text() if LOG_FILE.exists() else ""
    metrics = parse_log(log_text)
    metrics["wall_s"] = wall_s
    metrics["returncode"] = proc.returncode
    metrics["matched_via_poll"] = matched_at is not None

    sess = metrics["session_started_ts"]
    last = metrics["last_auto_expand_ts"]
    if sess is not None and last is not None:
        metrics["tree_mount_ms"] = (last - sess) * 1000.0
    else:
        metrics["tree_mount_ms"] = None
    return metrics


def run_repo(
    repo: Path,
    runs: int,
    *,
    expected_trees: int,
    poll_interval_s: float,
    settle_after_match_s: float,
    timeout: int,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for i in range(runs):
        r = run_once(
            repo,
            expected_trees=expected_trees,
            poll_interval_s=poll_interval_s,
            settle_after_match_s=settle_after_match_s,
            timeout=timeout,
        )
        wall = r.get("wall_s") or 0.0
        print(
            f"  run {i + 1}/{runs}: tree_mount_ms={r.get('tree_mount_ms')}"
            f"  rows={r.get('last_rows')}  trees_settled={r.get('auto_expand_count')}"
            f"  ft_log_lines={r.get('filetree_lines')}"
            f"  via_poll={r.get('matched_via_poll')}  wall={wall:.2f}s"
        )
        rows.append(r)

    mounts = sorted(
        float(r["tree_mount_ms"]) for r in rows if r.get("tree_mount_ms") is not None
    )
    median_mount_ms: float | None
    if len(mounts) >= 3:
        # Drop fastest + slowest before taking median.
        median_mount_ms = statistics.median(mounts[1:-1])
    elif mounts:
        median_mount_ms = statistics.median(mounts)
    else:
        median_mount_ms = None

    return {
        "median_mount_ms": median_mount_ms,
        "raw_mounts_ms": mounts,
        "runs": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--repo",
        action="append",
        required=True,
        help="Repository to benchmark (repeat for multiple).",
    )
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument(
        "--expected-trees",
        type=int,
        default=1,
        help="How many 'tree mount settled' lines to wait for. Default 1 "
        "covers the main side-panel tree, which is always present; pass 2 "
        "to also wait for the Active Changes panel's embedded tree (which "
        "only mounts when the working tree is dirty AND the GitController "
        "scan has populated changedPathSet before the panel renders).",
    )
    ap.add_argument("--poll-interval-ms", type=int, default=100)
    ap.add_argument(
        "--settle-after-match-ms",
        type=int,
        default=700,
        help="Sleep window after we see the expected lines, before SIGTERM, "
        "so the Logger flush timer has one more cycle.",
    )
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--label", default="run")
    ap.add_argument("--out", type=Path, default=IDE_ROOT / "bench" / "results.json")
    args = ap.parse_args()

    results: dict[str, dict[str, Any]] = {}
    for raw in args.repo:
        repo = Path(raw).expanduser().resolve()
        if not (repo / ".git").exists():
            print(f"[bench] WARN: {repo} has no .git directory", file=sys.stderr)
        print(f"[bench] repo: {repo}")
        results[str(repo)] = run_repo(
            repo,
            args.runs,
            expected_trees=args.expected_trees,
            poll_interval_s=args.poll_interval_ms / 1000.0,
            settle_after_match_s=args.settle_after_match_ms / 1000.0,
            timeout=args.timeout,
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "label": args.label,
        "expected_trees": args.expected_trees,
        "runs_per_repo": args.runs,
        "results": results,
    }
    args.out.write_text(json.dumps(payload, indent=2))
    print()
    print(f"[bench] wrote {args.out}")
    print(f"[bench] === summary ({args.label}) ===")
    for repo_path, data in results.items():
        repo_short = Path(repo_path).name
        med = data["median_mount_ms"]
        med_str = f"{med:.0f}ms" if isinstance(med, (int, float)) else "n/a"
        print(f"  {repo_short:25} median tree_mount = {med_str}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
