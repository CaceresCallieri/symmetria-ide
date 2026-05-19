---
name: post-TIOCSCTTY fzf lag — RESOLVED
description: Ctrl+E and fzf invocations work fast (~100ms); root cause was pyte not answering DSR/DA1 queries; fix shipped via _AnswerbackHistoryScreen
type: project
---

## Resolved 2026-05-19

The 2-3s lag (in fact: complete hang in headless tests) when Ctrl+E
fired the zoxide widget was caused by pyte's `Screen` having a NO-OP
`write_process_input`. `zoxide query -i` invokes fzf with `--height=40%`,
which uses Device Status Report (`ESC [ 6 n`) to find the cursor and
draw relative to it. Without a reply, fzf hangs. Stock pyte builds the
correct reply string in `report_device_status(6)` but never sends it
because `write_process_input` is a no-op by design.

**Fix** (`src/symmetria_ide/terminal_backend.py`):
`_AnswerbackHistoryScreen` subclass overrides `write_process_input` to
encode the reply as latin-1 and route it through a callback. The
callback wired in `TerminalBackend.start()` is `self.write`, which
already serialises via `_stdin_lock` against main-thread keystroke
writes (so the response can't interleave with a concurrent user
keystroke mid-CSI).

**Empirical results** (`PYTHONPATH=src python tools/diag_fzf_lag.py`):
- `zoxide query -i` direct invocation: ~18ms (was: timeout at 5s)
- Ctrl+E end-to-end → fzf rendered: ~114ms (was: timeout at 10s)
- All 693 tests pass; 3 new regression tests pin DSR (`ESC [ 6 n` →
  `ESC [ 1;1 R`), DA1 (`ESC [ c` → `ESC [ ? 6 c`), and the
  no-callback-noop defensive default.

## What this also unblocks (not yet measured)

The same fix unblocks any TUI that issues DSR or DA1 at startup:
vim's xterm-bg detection, less's terminal sizing fallback, btop's
capability probe, and any program using libreadline's `readline()`
which queries DSR on certain code paths. None of these were
previously known to be broken in our terminal, but if a user reports
"X TUI doesn't render correctly", check it now works.

## Diagnostic script kept

`tools/diag_fzf_lag.py` — invokes term_repl headless, sends Ctrl+E,
times fzf header rendering. Re-run if a future regression breaks this.

## Pointers (read these first if regression suspected)

- `src/symmetria_ide/terminal_backend.py` — `_AnswerbackHistoryScreen`
  class + the `start()` callsite. Subclass docstring carries the full
  rationale; do not delete it.
- `tests/test_terminal_backend.py` — three `test_answerback_*` tests
  pin the DSR/DA1 contract against future pyte releases.
- `tools/diag_fzf_lag.py` — runnable end-to-end smoke test.

## Falsified hypotheses (from earlier investigation)

- **Wayland Qt eats Ctrl+E** — falsified. `SYMMETRIA_IDE_KEY_TRACE=1`
  proved `\x05` reaches `keyPressEvent`.
- **TIOCSCTTY missing** — was a real bug, separate fix (`6cff30b`).
  `/dev/tty` access works correctly after that landed.
- **Bindkey was vicmd-only or missing** — falsified. `bindkey '^E'`
  inside the spawned shell shows `"^E" _zoxide_interactive_widget`
  with `main` aliased to `viins`.
- **fzf itself doesn't render under pyte** — falsified. Direct
  `fzf < /etc/passwd` renders in ~30ms (fullscreen fzf doesn't need
  DSR). Only `--height` mode needs the fix.

## What about the "double-press" inconsistency the user noticed?

User reported "sometimes the first Ctrl+E does nothing and a second
press is needed". With the DSR fix the first-press case now works
empirically; if it ever resurfaces, the most likely cause is zsh
ending up in `vicmd` mode after fzf Escape (the user's
`bindkey '^E' _zoxide_interactive_widget` binds in `viins` only after
`bindkey -v`). Fix would be a `bindkey -M vicmd '^E' ...` in the
user's `~/.zshrc:128` — their config, not ours.
