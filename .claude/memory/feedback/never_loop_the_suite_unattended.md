---
name: never-loop-the-suite-unattended
description: "Never run the full pytest suite repeatedly in an unattended loop — it killed the user's Claude session"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 1dc16f14-6524-437a-9b81-8d0fde68876c
  modified: 2026-07-28T03:09:42.661Z
---

**Never run this project's full pytest suite repeatedly in an unattended loop.**
Run it ONCE. If it fails, investigate that failure — do not re-run it two or
three times in a single `for` loop hoping for a clean pass.

**Why:** doing exactly that on 2026-07-28 killed the user's Claude Code session.
The command was a `for i in 1 2; do … pytest tests/ …; done` issued to confirm a
known-flaky suite would go green; run 1 passed (1606), and during run 2 the
whole shell died with **exit 137 (SIGKILL)**, taking the session's host with it.
The user had to point out what had happened.

The exact mechanism was NOT established, and the honest position is that it
stays unknown rather than being guessed at. What was checked and RULED OUT
afterwards:

- no kernel OOM kills in `journalctl -k` for the window,
- `systemd-oomd` is **inactive** on this machine, so no pressure kills,
- `tests/conftest.py::_isolate_agent_tmux` was intact (the documented
  suite-kills-its-own-session path, whose signature is also exit 137).

What was true at the time, and is the part worth acting on: the machine was at
**26 GiB of 30 GiB used with ~690 MiB free**, load 7–16, with an unrelated
agent's Playwright run holding 700%+ CPU and a 12 GiB peak. Each suite run
loads PySide6/Qt and spawns many threads. Stacking runs onto that is the
avoidable part regardless of which layer pulled the trigger.

**How to apply:**

- One suite run per verification point. A second run is a decision to make
  deliberately, after looking at *why* the first failed — never a retry loop.
- **Check the machine before any long/repeated run**: `cut -d' ' -f1-3
  /proc/loadavg` and `free -h`. Under heavy load or low available memory,
  defer, or run only the affected test files.
- Prefer the targeted subset (`pytest tests/test_foo.py`) over the full suite
  while iterating; the full suite is for the final check.
- To characterise a genuinely flaky test, run **that file** in a loop, not the
  whole suite — and say so before doing it, so the user can stop you.
- Exit 137 from a suite run is never "just a flake": stop, report it, and
  investigate before running anything else. It is the same signature as the
  documented [processEvents crash](../reference/qt-pyside/processevents_shared_app_segv.md)
  hazard and as the tmux-isolation failure described in
  `tests/conftest.py::_isolate_agent_tmux`, so it always deserves a look.

Related: the suite has a real, pre-existing intermittent failure in
`test_agent_events.py::_pump_events` — the thing that tempted the retry loop in
the first place. It is load-sensitive; see
[processevents-shared-app-segv](../reference/qt-pyside/processevents_shared_app_segv.md)
for the measurements and the two fixes already tried. Chasing it with repeated
full-suite runs is precisely the wrong move.
