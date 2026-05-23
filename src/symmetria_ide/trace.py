"""Lightweight startup-phase tracer for diagnosing time-to-interactive.

Gated by the `SYMMETRIA_IDE_TRACE` env var so production launches pay
zero cost. When enabled, `trace(name)` writes one line per phase to
stderr in the form:

    [TRACE] <ms_from_process_start> <phase_name>

The `T0` reference is captured at module import time (which is at the
very top of `__main__.py`, before any other heavy work like importing
PySide6 / pynvim happens). That lets the bench attribute the early
"Python interpreter + imports" cost as a phase of its own, separate
from the "QGuiApplication + QML engine + Main.qml load" cost the
existing Logger-based bench can see.

The traced phases form a coarse waterfall:

    process_start (T0=0)
    -> imports_basic_done
    -> qgui_created
    -> qml_registered
    -> controller_created
    -> engine_loaded         (Main.qml parsed; root objects live)
    -> start_begin
    -> backend_started       (nvim subprocess up + initial UI attached)
    -> start_done
    -> exec_entered          (Qt event loop running)
    -> [QML Logger "Session started" — emitted by Logger.qml.onCompleted]
    -> first_capsule         (first nvim capsule observed in Python)
    -> first_displayed_root  (AppController.displayedRoot signal fired)
    -> git_ignored_published (GitController emits first ignoredPathSet)
    -> [FM Logger "tree mount settled" — last side-panel tree settled]

The bench captures stderr to a temp file and merges trace lines with
the FM Logger lines to produce a single waterfall report.
"""

from __future__ import annotations

import os
import sys
import time

# Captured at the FIRST import of this module — which `__main__.py` does
# before importing PySide6 / pynvim / the heavy bits, so this is as close
# to "process start" as we can get from Python without an LD_PRELOAD shim.
T0: float = time.monotonic()

_ENABLED: bool = os.environ.get("SYMMETRIA_IDE_TRACE", "").strip() not in (
    "",
    "0",
    "false",
)


def trace(name: str) -> None:
    """Emit one trace line if tracing is enabled. No-op otherwise.

    Cost when disabled: one env-var lookup at import + one Python-level
    function call + one boolean check per call site. No I/O.
    """
    if not _ENABLED:
        return
    _elapsed = (time.monotonic() - T0) * 1000.0
    sys.stderr.write(f"[TRACE] {_elapsed:8.1f} {name}\n")
    sys.stderr.flush()


def elapsed_ms() -> float:
    """Return the milliseconds elapsed since `T0` (process import time).

    Useful when callers want to compute an interval that includes both
    a trace emit and a later assertion / log line — they can capture
    `elapsed_ms()` at the boundary and reuse the value.
    """
    return (time.monotonic() - T0) * 1000.0
