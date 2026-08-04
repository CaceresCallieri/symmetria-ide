"""Diagnostic: measure how long Ctrl+E takes to render fzf in our terminal.

Spawns the headless `term_repl` driver, sends Ctrl+E (which fires zoxide's
zle widget — `bindkey '^E' _zoxide_interactive_widget` in ~/.zshrc:128),
and times the wall-clock interval between the keystroke and fzf's counter
row (`27/27`) appearing in the pyte viewport. Interpretation:

    elapsed_ms ~= 2000   capability-query timeout (DSR / DA1 / OSC 11)
                         is the most likely cause. Fix: pyte subclass
                         that responds to these queries via a write-back
                         callback into the PTY master.

    elapsed_ms ~=  200   lag is elsewhere — fzf intrinsic startup, pyte
                         parsing cost, or Qt paint-thread back-pressure.
                         Profile next.

    timeout              fzf never rendered. Verify TIOCSCTTY fix is in
                         place (terminal_backend.py:434-448) and that
                         the user's zoxide widget binds Ctrl+E in viins.

Run:
    PYTHONPATH=src python tools/diag_fzf_lag.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"

# fzf always renders a counter row (matched / total). Word boundaries
# prevent false matches against cwd path fragments like "/12/345".
FZF_COUNTER_RE = r"\b\d+/\d+\b"

# Echo + wait sentinel proves the shell processed input — stronger than
# matching a prompt glyph, portable across Starship / pure / p10k themes.
PROMPT_SENTINEL = "DIAG_PROMPT_READY_42"


def send(proc: subprocess.Popen[str], **payload: Any) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()


def recv_for(proc: subprocess.Popen[str], req_id: str) -> dict[str, Any]:
    """Read events until one with matching id arrives; echo out-of-band
    events (`closed`, errors with no id) to stderr for diagnostics."""
    assert proc.stdout is not None
    while True:
        line = proc.stdout.readline()
        if not line:
            sys.exit("ERROR: term_repl stdout closed unexpectedly")
        event = json.loads(line)
        if event.get("id") == req_id:
            return event
        print(f"  (out-of-band: {event})", file=sys.stderr)


def main() -> int:
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            filter(None, [str(SRC_DIR), os.environ.get("PYTHONPATH", "")])
        ),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "symmetria_ide.term_repl"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        env=env,
        text=True,
        bufsize=1,
    )
    try:
        send(proc, id="s1", type="start", cwd=str(Path.home()))
        recv_for(proc, "s1")

        send(proc, id="w1", type="write", data=f"echo {PROMPT_SENTINEL}\n")
        recv_for(proc, "w1")
        send(proc, id="w2", type="wait_for", pattern=PROMPT_SENTINEL, timeout_ms=5000)
        ev = recv_for(proc, "w2")
        if ev["type"] != "match":
            print(f"FAIL: shell didn't echo sentinel: {ev}", file=sys.stderr)
            return 1
        print(f"shell ready in {ev['elapsed_ms']}ms (sentinel echo)")

        # \x05 == Ctrl+E. Pre-existing wait_for from above is cleared by
        # match-completion, so we can immediately register a new one.
        send(proc, id="w3", type="write", data="\x05")
        recv_for(proc, "w3")
        send(proc, id="w4", type="wait_for", pattern=FZF_COUNTER_RE, timeout_ms=10000)
        ev = recv_for(proc, "w4")
        if ev["type"] == "match":
            ms = ev["elapsed_ms"]
            print(f"\nfzf rendered in {ms}ms after Ctrl+E")
            if ms > 1500:
                print("  -> likely capability-query timeout (DSR / DA1 / OSC 11)")
            elif ms > 500:
                print("  -> moderate lag, below 2s timeout floor")
            else:
                print("  -> fast (no observable lag)")
        else:
            print(f"\nfzf did NOT render: {ev}")

        send(proc, id="snap", type="snapshot")
        snap = recv_for(proc, "snap")
        non_empty = [r for r in snap["rows"] if r.strip()]
        print(f"\n--- final viewport ({len(non_empty)} non-empty rows) ---")
        for row in non_empty[-15:]:
            print(row)
    finally:
        send(proc, id="bye", type="stop")
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
