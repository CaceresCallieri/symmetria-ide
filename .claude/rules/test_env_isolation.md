---
name: Isolate host-shared resources in tests by ENV, not just by mocking
description: Any test reaching a resource the running IDE also uses must have that resource's env var neutralised in an autouse conftest fixture; mocking subprocess.run alone is not enough.
paths: ["tests/**/*.py"]
---

A test that can reach a resource the *running* IDE also uses must be isolated at the **environment** level, in an autouse `tests/conftest.py` fixture — not only by monkeypatching `subprocess.run` or the calling function.

The resources this applies to, concretely (extend this list rather than reasoning by analogy):

| Resource | Neutralise |
|---|---|
| tmux substrate | `SYMMETRIA_IDE_AGENT_TMUX` (delete) + `SYMMETRIA_IDE_TMUX_SOCKET` (throwaway path) |
| agent bridge / events socket | `XDG_RUNTIME_DIR` (throwaway dir) |
| cross-IDE agent registry | `XDG_RUNTIME_DIR` (same fixture) |
| session store, tree cache | `XDG_STATE_HOME` (throwaway dir) |
| VPS server registry (fires live ssh probes) | `XDG_CONFIG_HOME` (throwaway dir) |
| subscription-usage poller (live HTTPS + `codex app-server` spawn) | `SYMMETRIA_IDE_USAGE_POLL=0` (set, not deleted — `0` IS the switch). ⚠ Gates `start()` only; `refresh()` is user-initiated and NOT gated, so a test reaching it must inject a fake pool (see `tests/test_usage_poller.py`) |

**Why:** The suite runs from inside an IDE agent pane — that is the normal workflow — so it inherits the IDE's environment and shares the developer's live state. Three individually harmless facts once compounded into the suite killing the session running it: the launchers export `SYMMETRIA_IDE_AGENT_TMUX=1` and the pane inherits it, so tests that never opted in enabled the tmux substrate; the socket resolver defaults to the real `~/.vigilia/tmux.sock`; and the generated tmux session name derives from the test process's cwd — this repo — so it collided exactly with the running agent's own session and `kill-session` landed on it. Per-test mocking did not prevent this: `monkeypatch` restores the env at teardown, so isolation only covers the test window, and any code that resolves the real path outside it (a deferred close, a teardown, a path that forgot to override) is pointed at live state. The symptom is also actively misleading — pytest dies with **exit 137 and no output**, which reads as an OOM or a hung test.

**How to apply:**

- Neutralise the env in an **autouse, function-scoped** fixture in `tests/conftest.py`, following the existing `_isolate_*` fixtures there. Point paths at a throwaway dir. Neutralise a flag toward its INERT value, which depends on the flag's polarity: **delete** it when mere presence is what enables (`SYMMETRIA_IDE_AGENT_TMUX`), **set** it when a value is what disables (`SYMMETRIA_IDE_USAGE_POLL=0`). Deleting a disable-flag re-enables the very thing you are containing. Either way, a test that wants the behaviour opts in with its own `setenv`/`delenv` — last writer wins.
- Still mock `subprocess.run` in tests asserting an argv. The env fixture is containment for what escapes; it is not a substitute for not shelling out.
- Adding a **new** env var that selects a host-shared resource means adding it to the fixture and to the table above in the same change.
- Neutralising a var makes its default branch unreachable from the suite. Add one focused test that deletes the var and asserts the default — that default is exactly the value whose reach causes incidents.
- When a test run dies mysteriously, do not infer the cause from the exit code. Shim the suspect binary on `PATH` (see `.claude/memory/reference/host/shim_the_binary_for_subprocess_forensics.md`) and run detached so your own timeout is not the killer.
