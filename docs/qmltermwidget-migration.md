# Migration: pyte terminal → qmltermwidget (forked) — execution handoff

**Status:** READY TO EXECUTE. The forked terminal widget is built, validated, and
committed. This doc is the self-contained plan for replacing the IDE's
pyte-based terminal renderer with the fork. Written for a fresh agent with no
prior conversation context.

## Why (one paragraph)

The IDE's terminal/editor surface currently runs NeoVim as a TUI inside a
**pyte** emulator we render ourselves (`terminal_view.py` = a Python
`QQuickPaintedItem` paint loop; `terminal_keys.py` = Qt→escape translation;
`terminal_backend.py` = PTY + pyte + reader thread). It works but: (a) pyte is
an incomplete emulator we keep patching (alt-screen, DECCKM, DA1/DSR answerback,
DCS stripping), and (b) the Python full-grid repaint is slow (~13.85 ms/frame on
a 200×50 grid, measured) and tore on up-scroll. We evaluated **qmltermwidget**
(Konsole's VT engine + renderer as a QML item — what cool-retro-term uses) and
it is dramatically better (fast, complete, correct). We **forked it** to fix a
transparency gap and to own the code for future extension.

## The fork (DONE — already built and validated)

- **Location:** `/home/jc/projects/symmetria-qmltermwidget`
- **Branch:** `symmetria` (off upstream `ce8e09a`, the commit Arch's
  `qmltermwidget 2.0.0.git1` packages). `upstream` remote → Swordfish90/qmltermwidget.
- **Commits:** `e34dfa7` (transparency: parse + apply + expose opacity),
  `706f0a7` (Symmetria colorscheme + MODIFICATIONS.md). License: GPL-2.0-or-later.
- **Build:** `cd /home/jc/projects/symmetria-qmltermwidget && qmake6 && make -j$(nproc)`
  → produces `QMLTermWidget/{libqmltermwidget.so, qmldir, color-schemes/}`.
- **Three upstream defects fixed** (see `MODIFICATIONS.md`): `ColorScheme::read`
  used `beginGroup("General")` which QSettings never matches for an INI
  `[General]` section (Opacity silently defaulted); `setColorScheme` never
  applied the scheme opacity; `setOpacity` wasn't QML-reachable. Net: background
  transparency now works, plus a live `backgroundOpacity` `Q_PROPERTY`.

### Install strategy
For now the IDE uses `engine.addImportPath("/home/jc/projects/symmetria-qmltermwidget")`
then `import QMLTermWidget 2.0` — works immediately, no system change. (Polish
later: a `PKGBUILD` that builds the fork and installs to
`/usr/lib/qt6/qml/QMLTermWidget/`, `provides`/`conflicts` the stock package.)
The stock `qmltermwidget` package is currently installed; our import path takes
precedence, but consider `pacman -R qmltermwidget` once the fork is packaged.

## Fork API (verified by introspection — use exactly these)

**`QMLTermWidget`** (a `QQuickPaintedItem`) key properties/methods:
- `colorScheme: "Symmetria"` — loads our scheme (ships in the fork's
  `color-schemes/`, discoverable via the import path). Setting it applies the
  scheme's 0.6 opacity (our fix).
- `backgroundOpacity` (real, OUR addition) — live background transparency
  control. **Do NOT use `opacity`** — that's `QQuickItem`'s render opacity and
  fades text too.
- `useFBORendering: false` — **REQUIRED for transparency** (the FBO path has no
  alpha → opaque). The image path is still C++-fast.
- `fillColor: "transparent"`, `font.family`, `font.pointSize`, `blinkingCursor`,
  `terminalSize` (QSize, read-only), `forceActiveFocus()`.
- Signals: `termGetFocus()`, `termLostFocus()`, `overrideShortcutCheck(QKeyEvent*, bool&)`
  (use to let IDE chords like Ctrl+Shift+E pre-empt the terminal).
- `session: QMLTermSession { ... }` — declared inline in QML.

**`QMLTermSession`** (the `KSession`; **drive from QML — PySide can't wrap
`KSession*`**, `obj.property("session")` raises "Can't find converter"):
- `shellProgram` (string), `shellProgramArgs` (stringlist),
  `initialWorkingDirectory` (string), `startShellProgram()` (call in
  `Component.onCompleted`).
- `currentDir` (string) — the shell's cwd. **No `currentDirChanged` signal** →
  poll on a `Timer`, or re-read on `titleChanged()`.
- `foregroundProcessName`, `getShellPID()`, `finished()` (→ `Qt.quit`/respawn),
  `started()`, `titleChanged()`, `sendText()`, `sendKey()`, `clearScreen()`.

### Working QML pattern (validated; mirrors the spike that the user approved)
```qml
import QtQuick
import QtQuick.Window
import QMLTermWidget 2.0

QMLTermWidget {
    id: terminal
    anchors.fill: parent
    anchors.margins: 20                       // Ghostty window-padding=20
    font.family: editorFontFamily             // context prop = default_font().family()
    font.pointSize: 8.5                        // Ghostty font-size; or Theme value
    colorScheme: "Symmetria"
    useFBORendering: false
    fillColor: "transparent"
    blinkingCursor: true
    session: QMLTermSession {
        id: sess
        shellProgram: "nvim"                  // or the shell, per pane
        shellProgramArgs: editorArgv          // context prop, see below
        initialWorkingDirectory: controller.displayedRoot
        onFinished: ...                        // editor: relaunch/quit; shell: log
    }
    Component.onCompleted: { sess.startShellProgram(); terminal.forceActiveFocus() }
}
```
The Window root must be `color: "transparent"` for the wallpaper blend (already is).
Ghostty baseline (from `~/.dotfiles/.config/ghostty/config`): black bg, opacity
0.6, JetBrainsMono Nerd Font, font-size 8.5, padding 20. Shipped scheme default
opacity = 0.6 (retune live via `backgroundOpacity`).

## Migration steps (staged; commit at each stage)

Branch: `qmltermwidget-editor` off `nvim-terminal-tui` (current IDE branch HEAD
`92258a8`).

### Stage 1 — editor pane
- `app.py` (`_build_engine`): `engine.addImportPath("/home/jc/projects/symmetria-qmltermwidget")`.
- Expose context properties for the nvim argv + socket. Currently the editor
  nvim is launched by `_editor_backend.start(cwd, argv=[...])` (a `TerminalBackend`)
  and `NvimBackend` connects to `self._nvim_socket`. KEEP the socket + NvimBackend
  entirely. Move the *launch* into the QML `QMLTermSession`:
  `shellProgram="nvim"`, `shellProgramArgs=["-n","--listen",<sock>,
  "--cmd","set rtp^=<runtime>","--cmd","luafile <runtime>/init.lua"]`
  (mirror `_RUNTIME_DIR` from `nvim_backend.py`; see the current `editor_argv` in
  `AppController.start`).
- `Main.qml`: replace `TerminalView { id: editor; backend: editorBackend }` with
  the `QMLTermWidget` pattern above (keep `id: editor` — ~30 focus/visibility refs
  depend on it; it's a FocusScope-like already). Re-parent `CommandLine` +
  `WhichKeyOverlay` onto it (proven to float over qmltermwidget — z-order works).
  Minimap stays gated off (`root.minimapEnabled === false`); it reads the socket
  `viewport` channel, unaffected.
- The `NvimBackend` socket-attach + all rpcnotify chrome (cmdline / which-key /
  capsules / completions / minimap data) is UNCHANGED — it rides the socket.
- Editor cwd still arrives via the nvim `cwd` capsule over the socket. UNCHANGED.

### Stage 2 — shell pane
- Replace the second `TerminalView { backend: terminalBackend }` with a
  `QMLTermWidget` whose session runs the shell (`shellProgram` = `$SHELL`,
  `initialWorkingDirectory` = `controller.displayedRoot`).
- **Shell cwd:** drop the OSC-7 shell-init injection (`runtime/symmetria-shell/`)
  and `_parse_osc7` — replace with reading `session.currentDir`. Wire it to
  `AppController` via a QML→Python call (e.g. a `Timer` in QML calling
  `controller.on_shell_cwd(sess.currentDir)` on change, or on `titleChanged`).
  `AppController` routes it through the existing `_route_capsule({id:"cwd", value})`
  path — same as today's `_on_terminal_osc7`. Keep that routing; only the source
  changes.

### Stage 3 — delete the old stack
- DELETE: `src/symmetria_ide/terminal_view.py`, `src/symmetria_ide/terminal_keys.py`.
- `terminal_backend.py`: the entire pyte/PTY/reader/answerback/alt-screen/DCS/
  screen-lock/cursor machinery is now dead. Either delete the file or reduce to a
  tiny helper if anything non-pyte is still referenced (check
  `_shell_launch_args` — the shell+OSC7 launch helper; with currentDir replacing
  OSC7, it's likely deletable). Grep `app.py` for `TerminalBackend`,
  `_editor_backend`, `_terminal_backend`, `editorBackend`, `terminalBackend`,
  `osc7`, `_on_terminal_osc7` and rework/remove.
- `term_repl.py` (headless TerminalBackend JSONL driver) — delete (depends on the
  pyte backend).
- `runtime/symmetria-shell/` OSC7 shell-init — delete (currentDir replaces it).
- KEEP: `editor_font.py` (font family/size source of truth — pass
  `default_font().family()` + point size to QML as context props), `nvim_backend.py`,
  all chrome models + QML (CommandLine, WhichKeyOverlay, minimap), `nvim_events`?
  (nvim_events was deleted in the nvim-terminal swap — confirm). StatusBarState etc.

### Stage 4 — tests + docs
- DELETE: `tests/test_terminal_view.py`, `tests/test_terminal_keys.py`,
  `tests/test_terminal_backend.py` (all pyte). Check `tests/test_minimap_view.py`
  + `test_app_controller_central_surface.py` for `TerminalView`/`TerminalBackend`
  refs and rework (the central-surface XOR + chord tests should survive, retargeted).
- ADD: a Main.qml structural test (QMLTermWidget present, session argv shape,
  import path added) + an offscreen smoke that the engine loads with the fork.
  QML-widget behavior is hard to unit-test; lean on structural + the live check.
- **CLAUDE.md:** rewrite "The terminal pane" section. REMOVE the now-obsolete
  pyte gotchas added recently: DCS/string-sequence stripping, the `_screen_lock`
  threading invariant, cursor-repaint, alt-screen, DA1/DSR answerback, the ANSI
  palette dual-source, `useFBORendering` note. REPLACE with: qmltermwidget-fork
  architecture, `backgroundOpacity`, `useFBORendering:false` for transparency,
  `currentDir` (no notify → poll/titleChanged), the import-path install, KSession*
  not-Python-wrappable (drive from QML), and a pointer to
  `/home/jc/projects/symmetria-qmltermwidget` + `MODIFICATIONS.md`. Update
  `terminal_keys.py`/`terminal_view.py`/`terminal_backend.py` entries in the
  Source layout section (deleted). Update "Runtime deps on Arch" to add
  `qmltermwidget` (the fork).

## Invariants (DO NOT BREAK)
1. The nvim `--listen` socket + `NvimBackend` rpcnotify chrome relay
   (cmdline / which-key / capsules / completions / minimap data) is the IDE's
   signature. It is INDEPENDENT of the terminal widget. Keep it intact.
2. Window root `color: "transparent"` + `useFBORendering:false` +
   `colorScheme:"Symmetria"` = the wallpaper blend. All three required.
3. Keyboard-first; the Ctrl+Shift+E (editor/terminal swap), Ctrl+Shift+A
   (anchor), Ctrl+Shift+T chords are `Qt.ApplicationShortcut` in Main.qml — they
   must still win over the terminal. `overrideShortcutCheck` is the fork hook if
   ApplicationShortcut precedence regresses.
4. Theme palette is the source of truth — the Symmetria scheme mirrors
   `qml/design/Theme.qml` / the old `_ANSI_PALETTE`.

## Verification
- `PYTHONPATH=src python -m pytest tests/ -q` green; `ruff check`/`ruff format`.
- Live (`PYTHONPATH=src python -m symmetria_ide`, lands on Hyprland workspace 6):
  editor shows nvim in qmltermwidget with **transparent Symmetria background**;
  `:` shows the IDE cmdline overlay; `<leader>` shows which-key; **scroll up = no
  tearing**; feels fast; shell pane works + cwd syncs the file tree; Ctrl+Shift+E
  swaps; status bar updates.

## Reference artifacts
- Working spike (if `/tmp` survived): `/tmp/qtw-spike/{run.py,Spike.qml}` — a
  standalone PySide6+QML harness running nvim in the fork with transparency. The
  QML pattern above is distilled from it.
- Today's IDE work (committed on `nvim-terminal-tui`, sealed): minimap hidden +
  neo-tree suppressed (`5382eb4`), DCS strip (`d20710f`), cursor repaint
  (`40f7554`), screen lock (`da3fdb6`), seal fixes (`92258a8`). The last four are
  pyte fixes that THIS migration makes obsolete — that's expected.
