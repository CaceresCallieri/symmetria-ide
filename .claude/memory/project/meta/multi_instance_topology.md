---
name: multi-instance-topology
description: "Multiple concurrent IDE instances (one per project, across Hyprland workspaces) is a deliberate design constraint — do NOT consolidate to single-process."
metadata: 
  node_type: memory
  type: project
  originSessionId: 3ff26fbf-0c89-4ea2-9688-5f9969a8d6c0
---

Symmetria IDE is meant to run as **many concurrent instances — one per project — across Hyprland workspaces**. Canonical rationale: `docs/vision.md` § "Parallel projects: the multi-instance constraint".

**Why:** The IDE's mainframe is agentic coding. Agents take 20+ min on heavy tasks (horizon lengthening), so the developer parallelizes across projects to avoid idle waiting. Each project lives in its own Hyprland workspace alongside its test env / browser / research — the IDE is one citizen of that workspace, not the container for it. As agents do more, fewer surfaces stay open *within* a project but *more projects* run in parallel. Concurrency trends up, not down.

**How to apply:** When doing perf/memory/architecture work, do NOT propose consolidating instances into a single-process multi-window design — it destroys per-project process + crash isolation and fights Hyprland's workspace model. The ~linear per-instance RAM cost is an accepted trade-off. The right lever is *per-instance* leanness (lean idle memory, defer sidecar pre-warm, one nvim per project via native buffers/tabs — not per file), never cross-instance sharing. `AppController`'s in-process slot-pool is for multiple agent sessions *within one project*, not for merging projects. This question was raised and settled in a 2026-06 memory analysis.
