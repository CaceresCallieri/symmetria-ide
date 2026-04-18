# Contributing

Symmetria IDE is a custom IDE wrapper around NeoVim built on PySide6 + QML. The project is currently solo; this file is the starting point once external contributions open up.

## Ground rules

Before writing code, read these. They override any habits you bring from other projects:

- **`.claude/project-standards.md`** — the authoritative style + quality ruleset consumed by the `/tech-debt` and `/code-review` skills. P0 rules are mandatory; P1 are strongly encouraged; P2 are recommended.
- **`CLAUDE.md`** — architectural context + 19 numbered gotchas burned in by past incidents. When project-standards.md cites `gotcha #N`, it means that entry. Do not "fix" gotcha-annotated code without understanding the incident it encodes — past agents have, and re-broken it each time.
- **`docs/dev-workflow.md`** — env vars for headless smoke testing, Hyprland workspace-6 rule, notification-system quirks.

## Dev setup

Arch Linux runtime deps:

```
sudo pacman -S --needed pyside6 python-pynvim
```

Dev tooling (one-time):

```
paru -S --needed ruff selene stylua python-pyright python-pip-audit
```

Run the IDE locally:

```
PYTHONPATH=src python -m symmetria_ide
```

Headless smoke test (no window; writes a screenshot and exits):

```
SYMMETRIA_IDE_SCREENSHOT=/tmp/out.png \
SYMMETRIA_IDE_TEST_KEYS='iHello<Esc>:w<CR>' \
SYMMETRIA_IDE_SETTLE_MS=800 \
PYTHONPATH=src python -m symmetria_ide
```

## Quality gates (project-standards §10)

Before opening a PR, run locally — the same commands CI will run:

```
ruff check src/ tests/
ruff format --check src/ tests/
pyright
pyside6-qmllint qml/*.qml
selene --config selene.toml runtime/
stylua --check runtime/
QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/ -v
```

Pyright baseline is 26 warnings — all documented PySide6-stubs false positives (see gotcha #7). Do NOT "fix" those by changing `@QAbstractItemModel` signatures — it breaks Qt's metaobject system. New warnings above 26 must be resolved before merge.

## PR checklist

- [ ] New behavior covered by a test (unit for pure math, `pytest-qt` for Qt-adjacent code)
- [ ] `ruff check` + `ruff format --check` clean
- [ ] `pyright` warning count unchanged vs `main`
- [ ] `pyside6-qmllint qml/*.qml` clean (no `unqualified` or `required` warnings)
- [ ] `selene` clean on any `runtime/**.lua` change
- [ ] Tests pass under `QT_QPA_PLATFORM=offscreen`
- [ ] If touching the render hot path (`nvim_view.py::paint`), verify zero new shiboken wrappers allocated per frame (gotcha #10)
- [ ] If touching `runtime/lua/orchestrator/whichkey/**`, verify against gotchas #15–#19
- [ ] CLAUDE.md updated if the change encodes a new invariant future agents would otherwise re-break

## Commit style

Conventional commits (`feat:`, `fix:`, `refactor:`, `chore:`, `docs:`, `test:`, `perf:`, `ci:`). Scope is optional but helpful (`fix(paint): …`). Subject ≤70 chars; body wraps at 72.

Do not reference AI assistance in commit messages.
