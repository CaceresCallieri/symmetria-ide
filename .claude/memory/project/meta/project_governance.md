---
name: project governance layer — standards, CI, and design tokens
description: symmetria-ide has a layered governance system (project-standards, CONTRIBUTING, CHANGELOG, pre-commit + CI, design-tokens singleton) that must be read before planning any non-trivial change
type: reference
originSessionId: a094e8d9-4dcb-4507-bc47-56d8a4453394
---
Before planning or implementing meaningful work in `~/projects/symmetria-ide`, read these in order. They are load-bearing for P0 rules that a generic plan would miss.

1. `.claude/project-standards.md` — the authoritative tiered rule set (P0 mandatory / P1 strong / P2 recommended) consumed by the `/tech-debt` and `/code-review` skills. Covers Python rules, PySide6 ↔ QML bridging, QML cleanliness, Qt threading, rendering hot path, pynvim + Lua, performance thresholds, testing, and tool commands. When a rule cites "gotcha #N" it means `CLAUDE.md`'s numbered gotcha list.

2. `CONTRIBUTING.md` — local quality-gate commands that CI runs too: `ruff check`, `ruff format --check`, `pyright` (report-only, baseline ~39 errors), `pyside6-qmllint qml/*.qml` (use `/usr/lib/qt6/bin/qmllint` directly — `pyside6-qmllint` may not be on PATH), `selene`, `stylua --check`, and `QT_QPA_PLATFORM=offscreen pytest`. Pre-commit hooks configured in `.pre-commit-config.yaml` run the relevant subset on staged files.

3. `CHANGELOG.md` — current Phase status at a glance. Update the Unreleased section alongside feature/fix commits.

4. `qml/design/Theme.qml` + `qml/design/qmldir` — singleton tokens (color, font, spacing, radius, size). **Chrome components must bind against `Theme.*` — palette/typography literals in component files are forbidden.** If a new surface needs a new token, add it to Theme.qml with a provenance comment (wine_theme source, etc.) FIRST, then reference it.

**Why:** The project is rigorously self-governed. Planning without reading these produces plans that miss P0 requirements (explicit queued connections with grep-able comments, daemon+Event shutdown, GC suspension around worker-thread emits, `@QmlElement` import anchors, Theme-token discipline) — and a code-reviewer agent will correctly flag them. The user has explicitly surfaced this expectation after a previous plan skipped the standards pass.

**How to apply:** On any Phase-sized change or new module in symmetria-ide: (1) read `project-standards.md` fully; (2) read `CONTRIBUTING.md` for the quality gates; (3) skim the relevant `docs/` entry; (4) check `CLAUDE.md` for gotchas touching your area. Only then draft the plan.
