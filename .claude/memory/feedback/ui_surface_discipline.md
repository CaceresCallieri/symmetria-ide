---
name: new UI surfaces start as simple functional placeholders
description: when building a new UI surface in symmetria-ide, default to a minimal placeholder bound to design tokens; defer aesthetic/interaction decisions until real data exists to iterate against
type: feedback
originSessionId: a094e8d9-4dcb-4507-bc47-56d8a4453394
---
When adding a new visual surface to Symmetria IDE (agent pane, file-manager drawer, browser pane, etc.), the first iteration ships as a **simple functional placeholder**:

- Minimal delegate, no cards/dividers/avatars, no decorative chrome.
- Bind against `Theme.*` tokens from day one; add new tokens (with provenance comments) to `qml/design/Theme.qml` before referencing them.
- Render the raw data as cleanly as possible — no clever grouping or collapsing yet.
- Keep scope narrow enough to land in a single commit-sized PR; deferred features (composer, drill-in, media rendering, focus switching) belong in follow-up iterations.

**Why:** The user said explicitly (and this has held up in practice): "chat interface is going to be a new UI, distinct from the terminal — let's keep it simple and functional, we'll think more deeply about the design decisions later." Designing richer interactions against guessed vocabulary is waste — the placeholder exists to expose real cadence and protocol shapes so follow-ups design against facts, not assumptions.

**How to apply:** When a new surface is proposed, the plan should (1) explicitly scope the placeholder deliverables, (2) list every interaction/visual the user might expect and explicitly defer each, (3) ensure the chosen shape is the simplest that exposes the real data. Do NOT propose full-featured surfaces up-front; the user will redirect and you will have wasted planning budget.

Phase 2's `AgentPane.qml` placeholder (flat event ListView, Theme tokens only, no turn grouping) is the canonical example of this pattern working.
