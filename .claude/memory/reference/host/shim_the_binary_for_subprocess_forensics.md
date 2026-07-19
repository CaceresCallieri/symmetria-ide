---
name: Shim the binary for subprocess forensics
description: When a process dies mysteriously, put a logging shim for the suspect binary on PATH instead of inferring the cause from exit codes
type: reference
originSessionId: 952b9964-294b-4f62-b7bb-8ae724b36084
---
When something dies and the cause is unclear, do not reason from the exit code — put a fake binary on `PATH` that logs its argv and exits 0, then re-run and read the log.

```sh
mkdir -p "$SCRATCH/shim"
printf '#!/bin/sh\necho "CALL: $*" >> "$LOG"\nexit 0\n' > "$SCRATCH/shim/tmux"
chmod +x "$SCRATCH/shim/tmux"
PATH="$SCRATCH/shim:$PATH" LOG=... <re-run the thing>
```

**Why:** Exit 137 (SIGKILL) was misread as an OOM kill for four consecutive runs of this project's test suite — memory pressure was real, so the hypothesis kept looking plausible, and each "verification" attempt re-ran the command and killed the user's Claude Code session again. `journalctl` showed no kernel OOM and no `earlyoom` kill, which should have falsified it sooner. A `tmux` shim answered the question in 20 seconds: the suite was invoking `kill-session` against the session the test run was itself living in. Exit codes describe the *effect*; the shim captures the *cause*, with arguments.

**How to apply:** Reach for this whenever a process is killed, hangs, or mutates state and the responsible call is not obvious — `tmux`, `ssh`, `pkill`, `systemctl`, package managers, anything a test or script shells out to. Two companions: run the suspect command **detached** (`setsid nohup … &` writing to a log file) so your own tool timeout cannot SIGKILL it and confuse the picture further, and remember a shim that always exits 0 will make failure-path tests report wrong results — read those failures as shim artifacts, not regressions. Related: [test env isolation](../../MEMORY.md) — a monkeypatched env is restored at teardown, so anything reaching a real socket/path afterwards is aimed at live state.
