# Symmetria IDE

> *The beauty in functionality and the functionality of beauty.*

A custom IDE being grown around NeoVim and the Symmetria ecosystem. Part of a cluster of personal tools — **Kosmos**, **Symmetria**, **Vigilia** — exploring how order emerges from chaos in the environments we work in daily.

## Status

**Phase 0 (Spine) complete.** The spine runs: a PySide6 window embedding NeoVim (`--embed`, msgpack-RPC), the grid rendered by a `QQuickPaintedItem`, and a native QML status bar wired to an `orchestrator.nvim`-style capsule stream.

```
$ sudo pacman -S --needed pyside6 python-pynvim
$ PYTHONPATH=src python -m symmetria_ide
```

Phase 1 (File Manager integration) is next.

## Concept

The terminal is functional but visually and interactively limited. This project asks: *what if the agentic, NeoVim-centric workflow lived inside a native UI that was built specifically around it?*

Goals:

- Embed NeoVim as the editor core (via `--embed` + msgpack-RPC).
- Host a native frontend for coding agents (starting with Claude Code).
- Integrate the Symmetria File Manager.
- Render images and HTML diagrams inline — things the terminal cannot show.
- Remain keyboard-first, minimal, and aesthetically consistent with Symmetria Shell.

## Where to read

See `docs/` for the full design:

- `docs/vision.md` — the long-horizon idea
- `docs/identity.md` — naming and design principles
- `docs/architecture.md` — embedding and extraction model (updated with realized Phase 0)
- `docs/tech-stack.md` — framework decision (PySide6 now, gpui later)
- `docs/phases.md` — the phased build plan (Phase 0 done, Phase 1 deferred, Phase 2 next)
- `docs/dev-workflow.md` — concrete commands: running, headless testing, workspace rules
- `docs/references.md` — projects that inform this one
- `docs/future.md` — years-out direction

`CLAUDE.md` is the fast-onboarding brief for agents and collaborators — source layout, capsule protocol, gotchas, Phase 2 starting points.

## Related projects

- Symmetria Shell — QuickShell-based desktop shell
- Symmetria File Manager — QML-based, will be integrated in Phase 1
- `orchestrator.nvim` — NeoVim plugin driving the Claude Code workflow

---

This is a personal, long-horizon project. Public for transparency, not for pull requests.
