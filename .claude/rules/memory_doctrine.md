---
name: Memory & knowledge management doctrine
description: How the three Anthropic-supported persistence mechanisms (CLAUDE.md, .claude/rules/, .claude/memory/) divide responsibility, plus the promotion lifecycle that prevents memory bloat.
---

# Memory & knowledge management doctrine

This project uses **three complementary Anthropic-supported mechanisms** to carry knowledge across sessions. Each has a distinct role; using the wrong one causes drift.

## The three mechanisms

| Mechanism | What goes here | Loaded when |
|---|---|---|
| `~/.claude/CLAUDE.md` (user-global) and `<project>/CLAUDE.md` (project root) | Global instructions, project architecture, "always do X" project-wide rules | Loaded into every session's context. Adherence degrades as either file grows — keep them focused on architectural facts, not accumulated content. |
| `.claude/rules/*.md` | Behavioral rules — optionally scoped via `paths:` frontmatter | Every session (unscoped) or on demand when matching files are touched (scoped) |
| `.claude/memory/` | Auto-memory: Claude's accumulated learnings, references, project state. `MEMORY.md` is the index; topic files in subfolders load on demand | Approximately the first 200 lines / 25KB of `MEMORY.md` every session (working heuristic, not a guaranteed API contract); topic files on demand |

## Choosing the right mechanism

Ask: **what kind of statement is it?**

- **"Always do X" / "never do Y"** in a behavioral sense, possibly scoped to file types → `.claude/rules/`
- **A learned reference / pitfall / past decision** Claude should recall when relevant → `.claude/memory/<area>/`
- **A project-wide architectural fact** every session needs (build commands, module names, repo layout) → `CLAUDE.md`

If it's both a rule and a learned reference, default to `.claude/rules/` — rules outrank memory when it comes to behavior. (This applies to knowledge you're relocating or classifying, not first writes — new observations start at Scratch regardless.)

## Promotion lifecycle (against bloat)

Knowledge has a lifecycle. Move it forward as it stabilizes; never leave everything in memory forever.

1. **Scratch** — observed once in conversation. Don't write yet; let it pass unless the user marks it as a decision or asks you to remember it.
2. **Memory** — explicitly requested by the user, or confirmed useful in a second independent context: write a topic file under `.claude/memory/<area>/`, add one bullet to `MEMORY.md` (≤200 chars). When unsure whether something merits memory, default to NOT writing — ask the user instead.
3. **Rule** — when a memory becomes "always do X" or "never do Y": move the file to `.claude/rules/<name>.md`, drop the `type:` and `originSessionId:` frontmatter fields (those are auto-memory fields — memory files keep them; rule files do not), optionally add `paths:` for file-scoped triggers, **remove the bullet from MEMORY.md**.
4. **Pointer** — when a system ships and is documented elsewhere (code, scripts, CLAUDE.md): shrink the memory file to a one-line pointer (`see src/symmetria_ide/X.py` or `see qml/Y.qml`), keep the file in `project/shipped/`, **remove its bullet from MEMORY.md** (it lives under the consolidated shipped-systems bullet).

The lifecycle exists because `MEMORY.md`'s first 200 lines load every session — every stale bullet there steals adherence from the rest.

If unsure which tier a piece of knowledge belongs in, default to `.claude/memory/` for now and flag the placement decision in the file's **Why** section. Do not silently promote to `.claude/rules/` or `CLAUDE.md` without user confirmation.

## MEMORY.md index discipline

- `MEMORY.md` is an INDEX, never memory content. Each entry is one bullet, ≤200 chars
  (raised from 150 on 2026-08-04: roughly 40 bullets exceeded the old figure while
  the file sat at 77 of its 200 allowed LINES, so the limit was policing the wrong
  budget — the line count is what decides whether the index loads): `- [Title](path/to/file.md) — one-line hook`. No frontmatter on `MEMORY.md` itself — the memory system reads it as plain markdown; frontmatter would render as stale content in the first lines.
- Headers (`## Section`) and blank lines count toward the 200-line budget. Use the section list in the current `MEMORY.md` as the canonical category set; don't invent new sections without (a) adding the new section name to the subfolder list below and (b) adding the new subfolder to the area list in this file.
- Topic files live in subfolders by area:
  - `feedback/` — user preferences and validated approaches (stable, project-wide)
  - `project/meta/` — identity, governance, key decisions
  - `project/active/` — operational state being maintained (Phase status, in-flight initiatives)
  - `project/shipped/` — past-tense system implementations (consolidated under one MEMORY.md bullet)
  - `reference/host/` — OS-level (Hyprland, QuickShell, notification stack)
  - `reference/nvim-rpc/` — NeoVim `--embed` + msgpack-RPC + pynvim quirks
  - `reference/qt-pyside/` — Qt 6 / PySide6 / QML / shiboken pitfalls
  - `reference/agent-sdk/` — `@anthropic-ai/claude-agent-sdk` + sidecar protocol

  Subfolders not yet populated remain empty (containing only a `.gitkeep`). Create them before writing a new topic file: `mkdir -p .claude/memory/<area>/`.
- Inside topic files, use markdown links to siblings (`[label](./sibling.md)`) for cross-references — auto-memory can navigate these on demand.
- Keep `MEMORY.md` ≤200 lines so the full index loads. If it grows beyond, prune shipped-system bullets first — consolidate them under one bullet pointing to the subfolder, e.g.: `- [Shipped systems](project/shipped/) — see subfolder for one-line pointers to completed Phase 0/1/2 components`.

## Rule file conventions

- Frontmatter: `name`, `description`, optionally `paths:` (glob array). Drop `type:` and `originSessionId` — those are auto-memory fields (`originSessionId` is the conversation UUID auto-stamped by Claude's memory tool; a diagnostic field only, not consumed by any tooling).
- Body: lead with the rule itself, then `**Why:**` and `**How to apply:**` sections so future agents can judge edge cases.
- A rule with `paths:` only loads when Claude reads files matching the globs — use this aggressively for language/tool-specific rules (e.g., `qml/**/*.qml`-only or `src/symmetria_ide/**/*.py`-only). Rules without `paths:` load every session (global scope). `memory_doctrine.md` itself is intentionally global.

## Anti-patterns to avoid

- Putting a "rule" in `MEMORY.md` instead of `.claude/rules/` — it'll load every session but won't carry the same weight as a rule, and the index gets noisier.
- Leaving shipped-systems bullets in `MEMORY.md` long-term — they become stale and squeeze out genuine reference knowledge.
- Cross-references via wikilinks `[[basename]]` instead of markdown links — wikilinks aren't navigable by Claude's tools without an explicit Read.
- Dumping everything into `CLAUDE.md` — degrades adherence for ALL its content. The numbered gotchas already strain the budget; new architectural facts should land in `reference/` memory and earn their way into CLAUDE.md only if every session needs them.
