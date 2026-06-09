# Contributing

Symmetria IDE is a custom IDE wrapper around NeoVim built on PySide6 + QML. The project is currently solo; this file is the starting point once external contributions open up.

## Ground rules

Before writing code, read these. They override any habits you bring from other projects:

- **`.claude/project-standards.md`** — the authoritative style + quality ruleset consumed by the `/tech-debt` and `/code-review` skills. P0 rules are mandatory; P1 are strongly encouraged; P2 are recommended.
- **`CLAUDE.md`** — architectural context + 25 numbered gotchas burned in by past incidents. When project-standards.md cites `gotcha #N`, it means that entry. Do not "fix" gotcha-annotated code without understanding the incident it encodes — past agents have, and re-broken it each time.
- **`docs/dev-workflow.md`** — env vars for headless smoke testing, Hyprland workspace-6 rule, notification-system quirks.

## Dev setup

Arch Linux runtime deps:

```
sudo pacman -S --needed pyside6 python-pynvim nodejs npm
```

Node `>=20` is required by the agent-pane sidecar (Arch's `nodejs` package ships >=22).

Dev tooling (one-time):

```
paru -S --needed ruff selene stylua python-pyright python-pip-audit
```

Sidecar build (one-time after clone, repeat whenever `sidecar/src/**` changes):

```
cd sidecar && npm install && npm run build && cd ..
```

This produces `sidecar/dist/index.js` (gitignored). `SessionHost` checks for it at startup and surfaces a clear error if missing. See `docs/dev-workflow.md` for details.

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
(cd sidecar && npm run typecheck && npm run build)
```

Pyright baseline is ~39 errors — all documented PySide6-stubs false positives (see gotcha #7). Do NOT "fix" those by changing `@QAbstractItemModel` signatures — it breaks Qt's metaobject system. Run `pyright 2>&1 | tail -1` on `main` to get the current baseline; new errors above that count must be resolved before merge.

The sidecar gates (`npm run typecheck` + `npm run build`) are mandatory whenever `sidecar/**` changes. `typecheck` runs `tsc --noEmit` against `tsconfig.json` (strict mode, `noUncheckedIndexedAccess`, `noImplicitOverride`). `build` rebuilds `dist/index.js` via esbuild — running it before commit prevents stale builds from drifting against source changes.

## PR checklist

- [ ] New behavior covered by a test (unit for pure math, `pytest-qt` for Qt-adjacent code)
- [ ] `ruff check` + `ruff format --check` clean
- [ ] `pyright` warning count unchanged vs `main`
- [ ] `pyside6-qmllint qml/*.qml` clean (no `unqualified` or `required` warnings)
- [ ] `selene` clean on any `runtime/**.lua` change
- [ ] Tests pass under `QT_QPA_PLATFORM=offscreen`
- [ ] If touching the render hot path (`minimap_view.py::paint` — the surviving `QQuickPaintedItem`, currently gated off in Main.qml), verify zero new shiboken wrappers allocated per frame (gotcha #10)
- [ ] If touching `runtime/lua/orchestrator/whichkey/**`, verify against gotchas #15–#21
- [ ] CLAUDE.md updated if the change encodes a new invariant future agents would otherwise re-break

## Commit style

Conventional commits (`feat:`, `fix:`, `refactor:`, `chore:`, `docs:`, `test:`, `perf:`, `ci:`). Scope is optional but helpful (`fix(paint): …`). Subject ≤70 chars; body wraps at 72.

Do not reference AI assistance in commit messages.
