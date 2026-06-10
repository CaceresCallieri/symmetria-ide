# Branch & worktree workflow — stable daily-driver vs. dev

The IDE is daily-driven while still under heavy iteration. To keep "the thing
being used" and "the thing being built" from being the same checkout, the repo
runs as **two git worktrees sharing one object database**:

| Tree | Path | Branch | Role |
|---|---|---|---|
| **dev** | `~/projects/symmetria-ide` | `dev` | Where ALL development happens — agents, experiments, reviews. This is the directory Claude Code sessions run in. |
| **stable** | `~/projects/symmetria-ide-stable` | `main` | The daily-driven build. Treated as read-only by agents; it only moves via promotion (below). |

Git refuses to check out the same branch in two worktrees, so dev work
*physically cannot* dirty the stable checkout — `main` is only reachable
through the stable tree, and `dev` only through this one.

## Rules for agents

1. **All code changes happen here, on `dev`** (or on feature branches off
   `dev`). Never edit files under `~/projects/symmetria-ide-stable/`.
2. **Never check out `main` in this tree** — it's claimed by the stable
   worktree; git will refuse anyway.
3. **Promotion is a deliberate user-initiated step**, not something to do
   automatically after a feature lands. Suggest it when dev has been verified
   in real use; don't perform it unprompted.

## Promotion procedure (dev → main)

Run when the user declares the current `dev` state trustworthy:

```sh
# 1. In the dev tree: make sure dev is clean and verified
#    (tests pass, ideally the work was sealed via /seal).
cd ~/projects/symmetria-ide && git status

# 2. In the STABLE tree (main is checked out there): merge dev.
#    Normally a fast-forward, since main only moves via promotion.
cd ~/projects/symmetria-ide-stable && git merge dev

# 3. Rebuild gitignored artifacts if the promoted range touched sidecar/:
cd sidecar && npm install && npm run build
```

The sidecar's `dist/index.js` and `node_modules/` are gitignored, so **each
worktree carries its own sidecar build**. A promotion that touches `sidecar/`
without step 3 leaves stable with a stale (or missing) agent backend — this is
the most likely "stable mysteriously broken" failure mode.

The forked qmltermwidget is NOT per-worktree state: both instances load the
pacman-installed `symmetria-qmltermwidget` package (see the comments in
`~/.local/bin/symmetria-ide`), so dev-side fork experiments only reach either
instance after an explicit `makepkg -sif` reinstall.

## Window classes & Hyprland routing

The Wayland `app_id` (= Hyprland window class) is set in `app.py::run` from
the `SYMMETRIA_IDE_APP_ID` env var, defaulting to `symmetria-ide`:

| Instance | Class | Hyprland rule |
|---|---|---|
| dev | `symmetria-ide` (default) | `workspace 6 silent` (in `~/.dotfiles/.config/hypr/windowrules.conf`) — dev launches stay penned on workspace 6 and never steal focus. |
| stable | `symmetria-ide-stable` (set by its launcher) | **No rule on purpose** — the daily driver opens on whatever workspace the user is on, like any normal app. |

The workspace-6 rule's regex is anchored (`^(symmetria-ide)$`), so it does not
accidentally match the stable class. Do not "generalize" the rule to a prefix
match — freeing the stable instance from workspace 6 is the whole point.

## Launchers

Both are machine-local (not stowed), following the existing pattern:

- `~/.local/bin/symmetria-ide` — dev tree, default class. Bound to
  **Super+Shift+Ctrl+Return** in `~/.dotfiles/.config/hypr/keybindings.conf`.
- `~/.local/bin/symmetria-ide-stable` — stable tree, exports
  `SYMMETRIA_IDE_APP_ID=symmetria-ide-stable`. Bound to
  **Super+Shift+Return** (the convenient chord belongs to the daily driver).
  Plus a desktop entry at
  `~/.local/share/applications/symmetria-ide-stable.desktop` so the Symmetria
  Shell app launcher can start it.

## Why worktrees instead of a second clone

Worktrees share refs and objects: a commit made on `dev` is instantly
mergeable from the stable tree with no fetch/push round-trip, and branch
state is visible across both (`git worktree list` from either side shows the
whole picture). A second clone would add a remote-sync step to every
promotion for no benefit.
