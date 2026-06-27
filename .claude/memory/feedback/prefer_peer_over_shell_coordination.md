---
name: prefer-peer-over-shell-coordination
description: "Cross-IDE / cross-instance state should use peer channels (shared file, direct socket), NOT routed through Symmetria Shell's bridge hub"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: def7b3b4-d371-49ea-9106-39f0f0fe3632
---

When two IDE instances (or any two Symmetria components) need to share state, prefer a **peer mechanism** — a shared tmpfs file, or a direct socket — over routing through Symmetria Shell's `agent-bridge.py` hub.

**Why:** The user explicitly rejected a shell-mediated design for cross-IDE account-usage sharing. A core IDE flow must not break when (a) the user runs a *different* shell, or (b) Symmetria Shell is down / restarting. State that has nothing to do with the shell (account usage, IDE-to-IDE coordination) should not depend on it. This also matches the project's own **agent-ownership inversion** trajectory — agent-activity capture already moved OFF the bridge onto the IDE's own socket (`agent_events.py`), and STT went direct shell→IDE. The bridge stays for what it's genuinely for: consolidating agents for the shell's OWN dashboard.

**How to apply:** For low-frequency shared state, a single tmpfs file under `$XDG_RUNTIME_DIR` (atomic write via `fs_atomic.atomic_write_json`, watched with `QFileSystemWatcher`, last-write-wins by an embedded timestamp) beats a socket mesh — no discovery, no N×N fanout, and it persists the last value even when every instance is closed. Reach for the bridge ONLY when the shell itself is the consumer. Don't propose shell-hub changes for IDE-to-IDE features. First shipped as the account-usage shared-file channel (`src/symmetria_ide/account_usage_store.py`). See [[multi_instance_topology]] — many concurrent IDE instances is deliberate, which is exactly why peer coordination matters.
