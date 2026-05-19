---
name: post-TIOCSCTTY fzf lag investigation
description: Ctrl+E now works (TIOCSCTTY shipped) but fzf has 2-3s startup lag; suspected unresponded terminal-capability queries; term_repl + wait_for ready to investigate
type: project
---

## State at 2026-05-19

**Fixed and shipped** (commits `6cff30b` + `e8a333b`):
- Ctrl+E opens zoxide's fzf picker. Root cause was `subprocess.Popen(start_new_session=True)` calling `setsid()` without making the slave PTY the controlling TTY. Without that, `/dev/tty` returned ENXIO inside zle widgets — fzf's UI silently rendered to stderr, which the widget redirected to `/dev/null`. Fix is a `preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0)` in `TerminalBackend.start()` with a 9-line comment explaining why.

**Open issue**: 2-3s lag for fzf launches via zle widget. The picker appears but takes 2-3 seconds vs near-instant in Ghostty. **Sometimes** the first Ctrl+E does nothing and a second press is needed.

## Lag — most likely cause (untested empirically)

fzf sends terminal-capability queries at startup (DSR `\e[6n`, DA1 `\e[c`, OSC 11 background) and waits for responses. pyte's `Screen` class has stub methods for `report_device_attributes` / `report_device_status` but they don't write replies back to the master fd. fzf hits its ~2s capability-detection timeout, then proceeds with defaults.

**Fix shape (if confirmed):** Subclass `pyte.HistoryScreen`, override `report_device_attributes` to write `\e[?6c` (VT102), override `report_device_status` (5)→`\e[0n` and (6)→`\e[<row>;<col>R`. Pass a write callback through the subclass so it can push bytes back to the PTY master via `TerminalBackend.write` (or a new private channel that doesn't go through the public Slot).

## Double-press inconsistency — most likely cause

After fzf closes via Escape, zle may end up in `vicmd` mode (user has `bindkey -v` in `~/.zshrc:5`). Ctrl+E binding at zshrc:128 is `bindkey '^E' _zoxide_interactive_widget` — installed in `main` which after `-v` aliases to `viins` only, NOT `vicmd`. So Ctrl+E in vicmd is unbound. User presses Escape (back to viins) implicitly via `i` or another key, then Ctrl+E works.

**Fix shape:** `bindkey -M vicmd '^E' _zoxide_interactive_widget` in user's `~/.zshrc` — but that's THEIR config, not ours. Could be a "if a future Ghostty parity audit names this, here's the answer" note.

## Falsified hypotheses (do NOT re-investigate)

- **Wayland Qt eats Ctrl+E** — falsified. `SYMMETRIA_IDE_KEY_TRACE=1` env var on `terminal_view.py` keypress logging proved `\x05` reaches `keyPressEvent` with text=`\x05` mods=Ctrl. The env-gated logging is permanent for future Ctrl-key bug reports.
- **Hyprland / Kanata / Symmetria Shell global shortcut on Ctrl+E** — checked `~/.config/hypr/`, `~/.config/kanata/`, `~/.config/quickshell/symmetria/`, `hyprctl binds`. None claims plain Ctrl+E (all E-binds require Super).
- **pyte can't render fzf** — falsified. `fzf < /etc/passwd` rendered correctly in the terminal pane.
- **zoxide DB empty** — falsified. `zoxide query --list` returned entries.
- **`event.text()` empty for Ctrl+letter on Wayland** — falsified. Ctrl+F, Ctrl+L, Ctrl+W all reached keyPressEvent with their respective control bytes populated in `text()`.

## Tools ready for the next session

- **`term_repl`** (commits `60357a6` + `e8a333b`): JSONL driver for `TerminalBackend`. Run via `PYTHONPATH=src python -m symmetria_ide.term_repl`. Commands: start, write, write_b64, resize, snapshot, wait_for, stop. Single-shell single-client.
- **`wait_for`** (commits `8290398` + `127c5b6`): regex-based screen-state polling with `elapsed_ms` in the match envelope. Subscribes to `screen_dirty` (not polling) for sub-millisecond latency. Single-watch contract; concurrent waits rejected with error envelope.
- **`SYMMETRIA_IDE_KEY_TRACE=1`**: env-gated keypress logging in `terminal_view.py:keyPressEvent`.

## Suggested next-step diagnostic (~30 LOC script using term_repl)

```python
# Spawn shell, time the bytes between Ctrl+E and fzf header appearing.
1. start (cwd=$HOME)
2. wait_for prompt pattern → confirms shell rendered
3. write \x05 (Ctrl+E)
4. wait_for fzf header pattern (e.g. `>\s+\|` or `\d+/\d+`) with timeout_ms=5000
5. report match["elapsed_ms"]
```

**If elapsed_ms ~2000ms** → confirms capability-query timeout. Fix the responder in pyte subclass.
**If elapsed_ms ~200ms** → lag is elsewhere (fzf intrinsic startup, GC pause, paint thread back-pressure). Investigate further.

## Pointers (read these first in the next session)

- `src/symmetria_ide/terminal_backend.py:434-448` — TIOCSCTTY fix + comment
- `src/symmetria_ide/term_repl.py` — full headless driver, ack-on-completion `wait_for` semantics
- `tests/test_term_repl.py` — 14 tests covering protocol mechanics + `_Interactive` harness for bidirectional tests
- CLAUDE.md gotcha section — TIOCSCTTY did NOT earn its own gotcha entry; the code comment is the breadcrumb
