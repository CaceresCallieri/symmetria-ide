# Project Standards — Symmetria IDE

> PySide6 (Qt 6) + QML + Python 3.14 + embedded NeoVim (pynvim msgpack-RPC) + Lua runtime overlay.
> This file is consumed by the `/tech-debt` and `/code-review` skills. Rules are grep-able wherever
> possible and tied to concrete CLAUDE.md gotchas ("#N") whenever the rule was burned into the
> codebase by a past incident.

## Stack

- **Python 3.14** — runtime version (actual running version on this machine). Minimum declared in `pyproject.toml`: `>=3.12`. Rules in this document that cite 3.14-specific behavior (notably GC/SEGV risk — gotcha #10) apply only when running 3.14. Free-threaded build is NOT used; GIL + cooperative threads.
- **PySide6 / Qt 6.5+** — GUI, QML, `QQuickPaintedItem` custom grid renderer.
- **QML (vanilla Qt Quick, NOT QuickShell)** — overlays, status bar, command line, which-key panel.
- **pynvim** — msgpack-RPC to an embedded `nvim --embed` child process.
- **Lua (LuaJIT)** — `runtime/init.lua` + `runtime/lua/orchestrator/**` plugin-style overlay.
- **pytest / pytest-qt** — test runner (offscreen Qt platform in CI).

## Critical Paths

Findings in these files/directories get prioritized. When two findings are equal severity, the one in a critical path wins.

- `src/symmetria_ide/nvim_view.py` — `QQuickPaintedItem` render hot path. Gotchas #10, #11, #12, #13, #14.
- `src/symmetria_ide/nvim_backend.py` — pynvim worker thread + signal boundary. Gotchas #1, #2, #9.
- `src/symmetria_ide/app.py` — `QGuiApplication`, QML engine, model wiring, shutdown order.
- `qml/*.qml` — any file here must pass qmllint and follow the property-binding rules below.
- `runtime/init.lua` — capsule emitter, completion pipeline, plugin neutralization (noice/nvim-cmp).
- `runtime/lua/orchestrator/whichkey/*.lua` — modal UI state machine. Gotchas #15, #16, #17, #18, #19.

---

# 1. Python Language Rules

### P0 — Required / Forbidden

- **REQUIRED: `faulthandler.enable(file=…)` armed at `__main__` import time.** Already in `__main__.py`. **Why:** Render-thread / pynvim-thread SEGVs otherwise leave zero traceback — you only see the process disappear. Gotcha #10. Check: `grep -n "faulthandler.enable" src/symmetria_ide/__main__.py`.
- **REQUIRED: `gc.freeze()` called exactly once, immediately before `app.exec()`.** **Why:** Moves startup-allocated objects into the permanent generation so they are never rescanned — collapses the race window that produced the 3.14 paint-thread SEGV. Gotcha #10. Check: `grep -n "gc.freeze" src/symmetria_ide/app.py`.
- **FORBIDDEN: bare `except:` or bare `except Exception:` that doesn't re-raise or log.** **Why:** Swallows `KeyboardInterrupt` / `SystemExit` and masks real bugs. Legal only as `except Exception as e: logger.exception(...); raise`.
- **FORBIDDEN: `from X import *` in library code.** Allowed only in `__init__.py` with an explicit `__all__`.
- **FORBIDDEN: `print()` outside `__main__` CLI code.** Use `logging.getLogger(__name__)`. `print` is unroutable, unfilterable, unstructured.
- **FORBIDDEN: `time.sleep()` polling loops for cross-thread waiting.** Use `threading.Event.wait(timeout=…)`. Fine for bounded retry/backoff.
- **FORBIDDEN: mutable default arguments (`def f(x=[])`).** Classic Python footgun. Use `x: list[int] | None = None; x = x or []`.
- **REQUIRED: every long-running thread is `daemon=True` OR owns an explicit shutdown `Event`.** **Why:** Non-daemon threads block interpreter exit — process hangs on Ctrl-C.
- **REQUIRED: `raise NewError(...) from original`** when re-raising inside `except`. `from None` only to deliberately hide chained noise.

### P1 — Strongly Encouraged / Discouraged

- **Prefer `@dataclass(slots=True, frozen=True)` for value objects** (especially anything crossing thread boundaries). **Why:** `slots` cuts memory 30–40% and eliminates `__dict__` (fewer GC-tracked dicts — directly relevant to gotcha #10); `frozen=True` makes the object safe to share without locks. `Cell` in `grid.py` is the canonical case.
- **Prefer PEP 695 generics (`class Foo[T]:`, `type Alias = ...`, `def f[T](x: T) -> T:`) over `TypeVar` + `Generic`.** 3.12+ native, scoped correctly, no module-level `T = TypeVar(...)` pollution.
- **Prefer `@override` (PEP 698) on every overridden method.** **Why:** Catches drift when the base signature changes — critical for Qt subclasses where `paintEvent`/`event` typos silently create dead methods.
- **Prefer `concurrent.futures.ThreadPoolExecutor` over raw `threading.Thread` for request/response work.** Raw threads remain appropriate for single long-lived workers (our pynvim loop is the correct shape).
- **Prefer `queue.Queue` over `list + Lock` for producer/consumer handoff.** It *is* the lock; `put/get` with `timeout` composes with shutdown events cleanly.
- **Prefer `pyright` over `mypy` for this project.** **Why:** PySide6 ships stubs that mypy rejects in strict mode; pyright handles them, is faster on incremental runs, and has better Qt type inference. Gotcha #7 notes: treat PySide6-stubs vs `QAbstractItemModel.data/rowCount/roleNames` disagreements as false positives — do NOT rewrite to match stubs, it breaks Qt's metaobject system.
- **Prefer narrowing `gc.disable()/enable()` to the smallest CPU-bound critical section.** Spanning I/O causes unbounded heap growth. The one around `_dispatch_redraw` in `nvim_backend.py` is the template.
- **Discourage: `from __future__ import annotations`** when `typing.get_type_hints` or `InitVar` is used with Qt types — PEP 563 stringifies annotations and resolution frequently fails on shiboken wrappers. Safe elsewhere.
- **Use `Protocol` (structural) over `ABC` (nominal)** unless you need `isinstance` or shared implementation. Ducks type-check; no forced inheritance.
- **Use `TypedDict` for RPC payloads, `dataclass` for internal models.** The capsule/whichkey payloads come off the wire as dicts; `TypedDict` describes them in place.

### P2 — Recommended

- **`ruff` is the single tool for lint + format.** Covers pyflakes, pycodestyle, isort, pyupgrade, bugbear. Do not install black/flake8/isort alongside — configuration divergence.
- **Line length 100.** 88 is cramped for typed code; 120 hides issues in side-by-side diff.
- **`src/` layout — already in place.** Forces install-before-test; catches packaging bugs locally.
- **`pytest.mark.parametrize` with `ids=[...]` for readable output.**
- **Structured logging via `logger.info("event_name", extra={"field": val})`.** Never f-string the payload into the message — filters can't destructure it back.
- **Drop `pylint` in 2026** — ruff covers ~95% of its useful rules. Keep `bandit` only if untrusted input is handled.
- **Python dev entrypoint:** `python -X dev -X tracemalloc=25 -m symmetria_ide` when chasing warnings or leaks.

---

# 2. PySide6 ↔ QML Bridging

### P0 — Required / Forbidden

- **REQUIRED: every `@Property` exposed to QML declares `notify=<signal>` and emits that signal on every mutation.** **Why:** QML binds against the NOTIFY signal; without it the binding is evaluated once and is forever stale. Gotcha #3. Example: `@Property(str, notify=modeChanged)` in `StatusBarState` with `self.modeChanged.emit()` in the setter.
- **FORBIDDEN: binding a QML property to a non-bindable method call** (e.g. `Text.text: model.rowCount()`, `capsules.get(0)`). **Why:** QML cannot hook non-bindable invocations; the value captures once and silently goes stale. Use a `Repeater { model: capsules }` or bind to a typed `@Property` instead. Gotcha #3.
- **REQUIRED: QObjects exposed to QML have a Python parent OR explicit `QQmlEngine.setObjectOwnership(obj, QQmlEngine.CppOwnership)`.** **Why:** QML's default `JavaScriptOwnership` will GC the wrapper, deleting the C++ object from under still-live Python references. Pattern: `self._controller = AppController(parent=self)` on the engine owner.
- **REQUIRED: `@Slot` methods invoked from QML declare `result=<type>` for non-void returns.** **Why:** Without it, PySide6 cannot marshal the return to JavaScript and QML sees `undefined`. Example: `@Slot(int, result=str) def label(self, idx): ...`.

### P1 — Strongly Encouraged

- **Prefer `@QmlElement` + `QML_IMPORT_NAME` over manual `qmlRegisterType`.** Supported path since PySide6 6.0, integrates with `pyside6-qmltyperegistrar` for stub generation.
- **Prefer `QAbstractListModel` subclass over `ListModel` or `property var []`.** **Why:** Row/role semantics give granular `dataChanged`/`rowsInserted`; JS arrays trigger full rebind on every change. `CompletionModel` in `app.py` is the template. Always bracket mutations with `beginInsertRows`/`endInsertRows` etc.
- **Split single-valued state from list-shaped state.** Put scalars on a `QObject` with one `@Property(notify=)` per field (`StatusBarState` pattern); put lists in a `QAbstractListModel` (`CapsuleModel`, `CompletionModel`). Mixing the two breaks binding semantics predictably — this is a project invariant.

### P2 — Recommended

- Name QML files by their top-level type (`WhichKeyOverlay.qml` → root `WhichKeyOverlay { ... }`). PySide6 loaders use filename-based lookup; deviating breaks it silently.

---

# 3. QML (Qt Quick) Rules

### P0 — Required / Forbidden

- **FORBIDDEN: committing QML files that produce `qmllint` findings of severity `error` or of warning category `unqualified`.** **Why:** Unqualified access is resolved at runtime with a perf penalty today and fails to compile under the QML→C++ compiler planned for Qt 6.11+. Fix: `id: root` at the outer item, reference `root.foo` not `foo`.
- **FORBIDDEN: `eval()` in QML JavaScript.** Prevents compiler optimizations; security hazard.

### P1 — Strongly Encouraged

- **Prefer typed QML properties (`property int foo: 0`) over `property var foo`.** **Why:** Typed properties let qmlcachegen lower bindings to C++ bytecode; `var` forces JS-engine dispatch.
- **Prefer `required property <type> name` on delegates.** Static-checked by qmllint; explicit role injection; survives context-property removal.
- **Discourage: `Repeater` for >50 items or any viewport-sized content.** Instantiates every delegate up-front. Use `ListView` with `reuseItems: true` and `cacheBuffer` for recycling.
- **Discourage: `layer.enabled: true` without a measured reason.** Allocates `width*height*4` GPU memory, disables batching, offscreen blending often costs more than the draw it replaced. Legitimate only for the lifetime of a shader/opacity effect, then disable.
- **Prefer `visible: false` over `opacity: 0`** when the item should not render or receive input. `visible: false` skips the entire subtree in the scene graph.
- **Prefer `Text.renderType: Text.NativeRendering` for small, static, non-transformed text.** QtRendering (distance fields) is only a win when scaling/rotating.
- **Discourage: `Text.RichText` / `Text.AutoText`.** Full HTML parser / detection overhead. Use `Text.PlainText` or `Text.StyledText`.
- **Discourage: `Timer { interval: 0 }` as a deferral mechanism.** Fires on the event loop and can beat render frames. Use `Qt.callLater()` for post-frame work; `NumberAnimation` / property-animation framework for visuals.
- **Discourage: `clip: true`.** Clipping is NOT an optimization — it allocates a `QSGClipNode` and breaks batching. Use `Text.elide`, opaque overlays, or layout redesign. Currently only 2 justified uses in `WhichKeyOverlay.qml`, `CommandLine.qml`.

### P2 — Recommended

- **Every QML root element has `id: root`.** Enables debugging, profiling, and id-based property lookup from children.
- **Reference properties as `root.foo`, not `parent.foo`.** `parent` is typed as `Item` and does not know custom properties; id-based references unlock typed access and compilation.
- **Use anchors over absolute positioning** — resolved by the C++ layout system, bypasses the JS binding evaluator.
- **Set `sourceSize` on non-icon `Image` elements.** Without it, full-resolution images stay in GPU memory.
- **Use `asynchronous: true` on `Image` for large files.** Decoding on the GUI thread blocks rendering.

---

# 4. Qt Threading & Signal/Slot Discipline

Background: the app has **three threads** — (1) GUI thread (Qt event loop, QML, paint dispatch), (2) pynvim worker thread, (3) `QSGRenderThread` (our `paint()` runs here — we do not start it).

### P0 — Required / Forbidden

- **FORBIDDEN: `Qt.BlockingQueuedConnection` anywhere.** **Why:** Same-thread use deadlocks instantly; cross-thread use deadlocks on any callback path. No use case in this codebase justifies it. Any `connect()` call passing this flag is a P0 review finding.
- **REQUIRED: cross-thread signals use `Qt.AutoConnection` (default) or explicit `Qt.QueuedConnection`.** **Why:** `Qt.DirectConnection` across threads runs the slot on the emitter's thread → violates QObject affinity, corrupts GUI state.
- **FORBIDDEN: `moveToThread()` on a QObject that has a parent.** Qt silently no-ops it. Construct as `Worker(parent=None)`, *then* `moveToThread(thread)`.
- **FORBIDDEN: subclassing `QThread` to implement work.** Slots on a `QThread` subclass execute on the *creator's* thread, not in `run()`. Use a plain `QObject` worker + `moveToThread`.
- **FORBIDDEN: deleting a QObject from a thread other than its affinity thread.** At shutdown: `worker.deleteLater()` from the GUI thread *after* `thread.quit(); thread.wait()`.
- **FORBIDDEN: `QCoreApplication.processEvents()` inside a slot, destructor, or `paint()`.** Re-entrancy corrupts state. For deferred work: `QTimer.singleShot(0, ...)` or `Qt.callLater`.
- **FORBIDDEN: emitting a signal while holding a `QMutex`.** Same-thread direct connection re-locks → deadlock.
- **FORBIDDEN: instantiating `QTimer` (or any event-driven QObject) on a thread without an active event loop.** `timeout` never fires; `deleteLater` never runs.
- **REQUIRED: every pynvim RPC call from non-worker code marshals through `nvim.async_call`.** **Why:** pynvim is not thread-safe; direct calls from the GUI thread raise `NvimError: request from non-main thread`. Gotcha #1. (See also Section 6 P0 for the pynvim-side enforcement.)
- **REQUIRED: shutdown sequence: `thread.quit()` → `thread.wait()` → release references.** Deleting a running `QThread` crashes the process.

### P1 — Strongly Encouraged / Discouraged

- **Prefer `@Slot()`-decorated methods as cross-thread signal receivers.** **Why:** Registers the method in Qt's metaobject; queued delivery uses C++ machinery instead of Python introspection. Forum reports describe Auto/Queued silently downgrading to Direct when the receiver is not a registered slot.
- **Prefer a context QObject as 3rd arg when connecting to a lambda:** `sig.connect(lambda x: self._h(x), self)`. **Why:** Without context, the connection has no lifetime tie — the lambda can fire against a half-destroyed receiver.
- **Prefer `QMetaObject.invokeMethod(obj, "slot", Qt.QueuedConnection, Q_ARG(...))` for one-shot cross-thread dispatches.** Documented thread-safe; no need to define a dedicated signal.
- **Prefer copyable, trivially-serialisable signal payloads (str, int, frozen dataclass, dict of primitives).** Queued connections copy args; non-trivial PyObject graphs stay reachable from both threads, expanding the GC race window (gotcha #10).
- **Discourage: reusing a `QObject*` across multiple `moveToThread()` calls.** Timer/socket state tied to the old dispatcher may leak.
- **Discourage: `QThread.terminate()`.** No cleanup chance, no mutex release. Use cooperative `requestInterruption()` + `isInterruptionRequested()`.
- **Discourage: `QEventLoop.exec()` to synchronously wait on a signal inside GUI code.** Nested loops re-enter slot dispatch. Tests: use `qtbot.waitSignal`. Production: restructure around async slot.

### P2 — Recommended

- **`Qt.UniqueConnection` for any connection set up in code that can run more than once.** Silent duplicate connections double-emit.
- **Check the return value of `disconnect()`** — `False` means no matching connection existed; otherwise you silently miss refactor drift.
- **Document every cross-thread `connect()` site with a one-line comment:** `# queued: NvimBackend worker -> AppController GUI`. Grep-able audit trail.
- **Expose worker public API as `@Slot`s only.** Forces callers through the signal / `invokeMethod` path, preserving affinity by construction.

### Connection Type Cheatsheet

| Property | Same thread | Different thread, async | Different thread, sync (BANNED) |
|---|---|---|---|
| `AutoConnection` resolves to | `DirectConnection` | `QueuedConnection` | n/a |
| Slot runs on | emitter | receiver | receiver; emitter blocks |
| Needs receiver event loop | no | **yes** | **yes** |
| Args copied | no | **yes** | no (by-ref) |
| Deadlock risk | slot re-entering sender | none | **high** |

### Anti-pattern Gallery

- `QThread` subclass with slots → slots run on creator's thread, not `run()`'s.
- `Worker(parent=self)` before `moveToThread` → parented, move silently no-ops.
- `QTimer` on a thread without `exec()` → `timeout` never fires.
- Lambda slot without context QObject → fires against destroyed receiver.
- `self._nvim.command(...)` from a GUI-thread slot → pynvim raises.
- `QColor(r,g,b)` inside `paint()` → shiboken wrapper churn + 3.14 GC race (gotcha #10).
- Holding a `QMutex` while emitting → same-thread receiver re-locks.
- `processEvents()` inside a slot / paint / destructor → re-entrancy corruption.
- `destroyed` handler touching the sender → QObject is mid-destruction.
- Sharing `QQuickItem` / `QWidget` refs across threads → GUI objects are main-thread only.
- Emitting from `__del__` → shutdown ordering is undefined.

---

# 5. Rendering Hot Path — `QQuickPaintedItem.paint()`

This is the most dangerous code in the project. Read CLAUDE.md gotchas #10–#14 before touching `nvim_view.py`.

### P0 — Required / Forbidden

- **FORBIDDEN: creating QObjects, starting `QTimer`s, or emitting signals inside `paint()`.** **Why:** `paint()` runs on the render thread; any QObject created there inherits render-thread affinity and can never be safely used from GUI-thread code.
- **FORBIDDEN: allocating PySide6/shiboken wrappers (`QColor(...)`, `QRectF(...)`, `QPen(...)`) inside `paint()` hot loops.** **Why:** Every wrapper is a GC root on Python 3.14; concurrent cyclic-GC on the worker thread can SEGV inside the render thread's C++ calls. Memoize (see `_rgb_to_qcolor` LRU in `nvim_view.py`) or mutate in place. Gotcha #10.
- **REQUIRED: frame driver gates on ALL animation sources.** `_animation_is_active()` returns True if scroll OR cursor OR blink is active. Disconnecting `frameSwapped` when one is done freezes the others. Gotcha #14.
- **REQUIRED: obey the scroll geometry invariants in gotcha #11.**
  - `max_delta` = `slot_start`, NOT `scrollback_rows - grid.rows`.
  - `SCROLLBACK_MULTIPLIER >= 3` for half-page scroll compounding.
  - Do not iterate `dr = grid.rows` when `pixel_residual_y >= 0` (stale-row leak).
  - Clip to exact `cols*cw, rows*ch`, not `boundingRect()`.
  - Per-frame order: `scroll_anim.tick()` → `_update_cursor_destination()` → `cursor_anim.tick()`.
- **REQUIRED: cursor animation spring stores the REMAINING DELTA, not absolute position.** Gotcha #12. Do not "fix" this to mirror `ScrollAnimation` — redirect-mid-flight semantics depend on the delta seeding.
- **REQUIRED: cursor blink uses `time.perf_counter()` wall clock, not per-frame accumulated `dt`.** Gotcha #13. Per-frame accumulation stair-steps opacity on compositor hiccup.

### P1 — Strongly Encouraged

- **Any new animation source must OR into `_animation_is_active()`.** Adding a 4th without wiring it = instant freeze regression.
- **Keep `paint()` branchless / data-driven where possible.** Run-coalescing by `hl_id` (already done) is the template: iterate once, compute runs, emit `fillRect` + `drawText` per run.
- **Cache any shiboken wrapper the hot path touches.** `QColor` (already done), `QRectF`, `QPen` are the next candidates if SEGV relapses.

---

# 6. pynvim + Lua (Embedded NeoVim)

### P0 — Required / Forbidden

- **REQUIRED: every pynvim RPC call from non-worker code marshals through `nvim.async_call(...)`.** Gotcha #1. The pynvim worker is the only thread allowed to touch `nvim.*` RPC methods directly. (See also Section 4 P0 for the Qt threading perspective.)
- **REQUIRED: after `nvim.subscribe("capsule")` and any other notification subscription, trigger an initial state push via `exec_lua("_G.symmetria_push_state()")`.** **Why:** `init.lua` runs during nvim startup, before Python has subscribed. Missed events are not buffered. Gotcha #2.
- **FORBIDDEN: `vim.fn.getcharstr()` / `vim.fn.input()` in any Lua modal UI reachable from `--embed`.** **Why:** Blocks nvim's main thread, starves RPC delivery, hangs both sides. Use event-driven keymaps + autocmds (the `orchestrator.whichkey.state` pattern). Gotcha #15.
- **FORBIDDEN: relying on `vim.schedule` / `vim.defer_fn` for cleanup after `feedkeys`-style sequences.** Scheduled callbacks don't fire while nvim is in `timeoutlen` prefix-wait with pending typeahead. Use `vim.cmd.normal{keys, bang = true}` — synchronous, returns after nvim has fully processed the keys. Gotcha #16.
- **REQUIRED: save-and-restore pre-existing keymaps via `vim.fn.maparg` + `vim.fn.mapset` when an ephemeral keymap (modal UI, etc.) installs at a slot.** **Why:** Third-party plugin maps (flash.nvim at `s`/`S`, etc.) get clobbered; `vim.keymap.del` on close leaves the slot empty. Gotcha #19. Skip `prev.buffer > 0` entries — `mapset` cannot honor `dict.buffer` and would promote buffer-local to global.
- **REQUIRED: trigger installers (the outer layer of any modal UI) self-heal by verifying each slot via `vim.fn.maparg`, NOT by trusting an internal install-cache.** **Why:** Menu keymaps overwrite trigger slots; internal caches lie after overwrite. Gotcha #17.
- **REQUIRED: `redraw` event handlers accept forward-compat arg tails.** Two patterns must stack: (a) known new positional args added by documented NeoVim releases get typed named defaults (e.g., `_hl_id: int = 0` in `cmdline_show` for the 0.10 addition); (b) a trailing `*_rest: Any` absorbs any further additions beyond what you've named, preventing crashes on version bumps. `cmdline_show` gained `hl_id` between 0.9 and 0.10; `grid_line` is gaining a wrap flag at 0.11. A signature with only named defaults and no `*_rest` will crash on the NEXT undocumented addition. Gotcha #9.
- **FORBIDDEN: catching broadly with `pcall` and dropping the error.** `local ok, _ = pcall(f)` silently swallows real bugs. Use `if not ok then vim.notify(err, vim.log.levels.ERROR) end`.
- **REQUIRED: when a plugin ecosystem claims the same `ext_*` extension we do, set `vim.g.symmetria_ide = 1` and document the off-switch.** Pattern borrowed from `g:goneovim` / `g:neovide`. Applies to noice.nvim, nvim-cmp cmdline/popupmenu, etc. Gotcha #8.
- **REQUIRED: `laststatus=0` must be re-asserted from a `VimEnter` autocmd, NOT only from `--cmd`.** **Why:** Plugin `setup()` calls (lualine, etc.) run after startup and overwrite `laststatus`, restoring the status line inside the viewport. `--cmd` fires too early and loses the race. Gotcha #5.

### P1 — Strongly Encouraged

- **Use `pynvim.attach("child", argv=["nvim", "--embed", ...])` for the IDE frontend.** `stdio` mode is for *plugin hosts* (nvim calling into Python). `socket`/`tcp` are for remote control only.
- **Call `nvim.ui_attach(cols, rows, {ext_linegrid=True, ext_cmdline=True, ext_popupmenu=True, rgb=True})`.** `ext_linegrid` is mandatory on 0.7+. Omit `ext_hlstate` unless you actually consume semantic highlight-group names — it increases `hl_attr_define` payload volume with no benefit when you only use RGB values. Avoid `ext_multigrid` unless you actually render windows separately — 3–4× event volume.
- **Batch with `nvim.request(name, *args, async_=True)` for fire-and-forget notifications.** Sync requests block the worker; `nvim_input`/`nvim_feedkeys` should always be async.
- **Drain `flush` before repainting.** The UI contract: buffer all `grid_line`/`grid_scroll`/`mode_change` into the Grid, trigger Qt update on `flush`. Painting mid-burst tears.
- **Namespace every Lua module under `lua/orchestrator/...` with `local M = {}; return M`.** No bare globals. Enables `package.loaded["..."] = nil` + force-reload during dev.
- **Annotate public Lua APIs with LuaCATS (`---@param`, `---@return`, `---@type`, `---@class`).** Consumed by lua-language-server, selene, and the which-key trie.
- **Use `vim.keymap.set(mode, lhs, rhs, {nowait=true, silent=true, desc="..."})` with explicit `desc`.** `desc` is the data source our which-key trie reads; missing `desc` means `<no desc>` in the menu.
- **Use `vim.notify(msg, vim.log.levels.WARN)` not `print`.** `print` dumps to `:messages`, skipping the user's notification pipeline (noice, mini.notify).
- **Rebuild the keymap trie on `BufEnter`, `LspAttach`, `LspDetach` — NOT `CursorMoved`.** Buffer-local maps only change at those boundaries. Over-rebuild costs ~5–15ms each and ruins scroll feel.
- **REQUIRED: any keymap trie for modal UI must be seeded from the which-key.nvim preset catalog in addition to `nvim_get_keymap`.** **Why:** `nvim_get_keymap()` does not return NeoVim's built-in motion chains (`gg`, `gU`, `gf`, `zz`, `[s`, `<C-w>h`, etc.) — these are hardcoded in the C core and are invisible to the API. A trie built purely from `nvim_get_keymap` will produce empty submenus under `g`, `z`, `[`, `]`, `<C-w>`. The preset catalog (`which-key.nvim`'s `lua/which-key/plugins/presets.lua`) fills this gap. **Lazy-load caveat:** the module is `event = "VeryLazy"` — retry on each rebuild until it returns a non-empty catalog, then cache; do NOT require it at VimEnter. Gotcha #18.

### P2 — Recommended

- **Prefer `selene` over `luacheck` for Lua linting** — Rust, actively maintained, ships a `neovim+luajit` std.
- **`stylua` for Lua formatting.** Pairs with selene.
- **Test Lua with `plenary.nvim` busted** or `nvim --headless -l test.lua` (0.10+).
- **Use `nvim_create_augroup("<ns>", {clear = true})` + `nvim_create_autocmd`** — never bare `:autocmd`. Clear-on-create makes reload safe.
- **Use `vim.on_key(callback, ns_id)` for observation only.** It CANNOT block keys — fires after the key is already in typeahead. Not an input filter.

### Version Compatibility

- `cmdline_show`: 0.9 = 6 args, 0.10+ = 7 args (adds `hl_id`). Absorb with `*_rest`.
- `ext_linegrid`: default-on for new UIs from 0.7. Legacy `put`/`cursor_goto`/`scroll` events only emitted when not set. Never mix.
- `grid_line`: wrap flag 8th arg lands at 0.11 (currently on master). `*_rest` already absorbs.
- `vim.keymap.set`: 0.7+. `vim.cmd.normal{...bang=true}` table form: 0.8+. `nvim_exec2`: 0.9+ (replaces `nvim_exec`).

---

# 7. Performance Thresholds

| Metric | Good | Warning | Bad |
|---|---|---|---|
| `paint()` wall time per frame | <4 ms | 4–8 ms | >8 ms (misses 120Hz) |
| PySide6 wrappers allocated in `paint()` | 0 | 1–10 | >10 per frame |
| Cross-thread `Qt.DirectConnection` count | 0 | 0 | any occurrence |
| QML items in a single `Repeater` | <20 | 20–100 | >100 (use ListView) |
| `layer.enabled: true` items on-screen | 0 | 1–3 | >3 simultaneous |
| qmllint warnings at CI | 0 | <5 | any `unqualified` or `required` |
| `property var` usage (count in `qml/`) | 0 | 1–5 | >5 |
| Time from `QGuiApplication()` to first frame | <400 ms | 400–800 ms | >800 ms |
| Python function length (lines) | ≤40 | 40–80 | >80 |
| Python module length (lines) | ≤400 | 400–800 | >800 |
| QML file length (lines) | ≤200 | 200–400 | >400 |
| Cyclomatic complexity (ruff `C901`) | ≤10 | 10–15 | >15 |
| Test coverage — critical paths | ≥90% | 80–90% | <80% |
| Test coverage — overall | ≥70% | 60–70% | <60% |
| `# type: ignore` per 1k LOC | ≤2 | 3–8 | >8 |

---

# 8. Testing

- **Offscreen Qt platform in CI:** `QT_QPA_PLATFORM=offscreen pytest tests/ -v`. No display server required; render loop still exercises, so render-thread regressions surface.
- **Prefer `pytest-qt` idioms over manual `QApplication`:** `qtbot.waitSignal(sig, timeout=500)`, `qtbot.waitUntil(lambda: ..., timeout=2000)`. Never `time.sleep`.
- **`pytest --qt-log-level-fail=WARNING`** — fail the run on any Qt warning.
- **Fixture scope:** `session` for expensive shared resources (`QApplication`, embedded nvim handle); keep default `function` for state-bearing fixtures.
- **`pytest.mark.parametrize` with `ids=[...]`** for readable output.
- **Pure-math unit tests have no Qt imports.** `test_scroll_animation.py`, `test_cursor_animation.py`, `test_grid.py`, `test_keys.py` are the template.
- **Headless smoke test env vars** (see `docs/dev-workflow.md`):
  - `SYMMETRIA_IDE_TEST_KEYS` — scripted keystrokes
  - `SYMMETRIA_IDE_SETTLE_MS` — settle time before screenshot/exit

---

# 9. Security

- **Never commit API keys, tokens, or credentials.** Pattern greps for `password|secret|api_key|token|private_key` should return nothing in tracked files.
- **Never ship `eval()` in QML** — compiler de-opt + injection vector. (Note: C++ plugin `.eval()` methods are distinct and fine.)
- **Never write to or read from paths outside `$XDG_*_HOME`** without explicit user-provided paths.
- **Shell scripts** must use `set -euo pipefail` (bash) or `setopt ERR_EXIT PIPE_FAIL` (zsh), and `command -v <tool>` before using external dependencies.
- **`faulthandler` crash logs** land in `$XDG_STATE_HOME/symmetria-ide/crash.log`. Never symlink that to a shared location — crash logs can contain pointer values and partial stack content.

---

# 10. Tool Commands

### Lint + format (Python)
- `ruff check src/ tests/` — lint.
- `ruff format src/ tests/` — format.
- `ruff check --fix --unsafe-fixes src/` — apply autofixes; review diff before commit.

### Type check
- `pyright` — prefer over mypy for PySide6; honors `pyrightconfig.json`.

### Test
- `PYTHONPATH=src python -m pytest tests/ -v`
- `QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest tests/ -v`
- `PYTHONPATH=src python -m pytest --cov=src/symmetria_ide --cov-report=term-missing --cov-fail-under=70`

### QML
- `pyside6-qmllint qml/*.qml` — static check (unqualified access, binding loops, required properties, type mismatches).
- `pyside6-qmltyperegistrar` — generate `.qmltypes` stubs from Python-registered types; feed into qmllint.
- `pyside6-qmlcachegen qml/Main.qml` — AOT QML bytecode; startup speedup + enforceable qmllint coverage.

### Qt scene-graph / render diagnostics
- `QSG_INFO=1` — backend, renderer, surface info at startup.
- `QSG_VISUALIZE=batches|clip|changes|overdraw` — render-time overlays. `batches` is the most useful "why is this slow" triage.
- `QSG_RENDER_TIMING=1` — per-phase (polish, sync, render, swap) wall-time log per frame.
- `QSG_RENDERER_DEBUG=render` — batch statistics.
- `QT_LOGGING_RULES="qt.qml.binding.removal=true;qt.scenegraph.*=true"` — targeted category logging.
- `QML_IMPORT_TRACE=1` — trace QML module resolution.

### Dependencies / security
- `pip-audit` — vuln scan against `pyproject.toml` + lockfile (free; `safety` requires an account now).
- `pacman -Qi pyside6 python-pynvim` — verify system-package versions on Arch.

### Lua
- `selene --config selene.toml runtime/` — lint.
- `stylua runtime/` — format.
- `nvim -V9/tmp/rpc.log` — verbose RPC log; decisive for "did the notification actually go out?" questions.
- `nvim --headless -l tests/<spec>.lua` — 0.10+ Lua test entry.

### Debug / dev-mode
- `python -X dev -X tracemalloc=25 -m symmetria_ide` — Dev mode: ResourceWarnings, stricter checks, memory origin traces.
- `faulthandler.enable()` is armed in `__main__.py` and writes to `$XDG_STATE_HOME/symmetria-ide/crash.log`.

---

# 11. Known Codebase Hotspots

A grep audit at the time of this document:

| Pattern | Count | Notes |
|---|---|---|
| `clip: true` | 2 | `qml/WhichKeyOverlay.qml`, `qml/CommandLine.qml` — likely justified (rounded corners / overflow); verify when touching. |
| `layer.enabled: true` | 0 | Clean. |
| `property var` | 0 | Clean. |
| `Qt.DirectConnection` (cross-thread) | 0 | Clean. |
| Python functions without return-type annotation | 4 | `QAbstractItemModel.data()` overrides in `app.py` (lines 159, 348, 439, 572) — intentionally un-annotated due to pyright stub conflict; see gotcha #7. Do not "fix" by adding return types — that breaks Qt's metaobject system. |
| `# type: ignore` | check before each release | Keep ≤2 per 1k LOC. |
| `nvim_view.py` module length | 1453 lines | Exceeds "Bad" threshold (>800). **Known hotspot** — `QQuickPaintedItem`, animation classes, and rendering helpers are co-located for paint-thread safety (see Section 5). Splitting across modules would require sharing mutable animation state across thread boundaries. Acceptable until a clean seam is identified. |
| `app.py` module length | 824 lines | Exceeds "Bad" threshold (>800). Refactor candidate for Phase 2: `StatusBarState`, `CapsuleModel`, `CompletionModel`, `WhichKeyState`, `WhichKeyModel`, and `AppController` could move to `models.py` and `controller.py`. |

When working near any non-zero row, consider addressing the instance. Do not regress the zero rows.

---

# 12. Documentation Discipline

- `CLAUDE.md` — agent-facing project context (architecture, commands, gotchas). The gotchas section is load-bearing; when a rule here references `#N`, it means CLAUDE.md gotcha N.
- `docs/vision.md`, `docs/identity.md`, `docs/architecture.md`, `docs/tech-stack.md`, `docs/phases.md`, `docs/references.md`, `docs/future.md` — durable design + planning.
- `docs/dev-workflow.md` — env vars for headless smoke testing, Hyprland workspace-6 rule, notification-system quirks.
- **Regression comments:** when a code-review fix causes a regression that must be reverted, add an inline comment at the affected code explaining what was tried, why it broke, and why the current approach is correct. Future agents will attempt the same "improvement" without this context. Also update CLAUDE.md or this file when the lesson is general.

### Environment / Deployment Notes

- **Window `app_id` for Hyprland:** `QGuiApplication.setDesktopFileName("symmetria-ide")` sets the Wayland `app_id`, which becomes the Hyprland window class. Required for `windowrule = workspace 6 silent, class:^(symmetria-ide)$` to match. Without it the rule targets the process name, which may vary by Python invocation. Gotcha #4.
- **Notification daemon is Symmetria Shell (QuickShell), not swaync/mako.** `swaync-client` and `makoctl` are no-ops on this system. Any code or script that calls these tools will silently do nothing — test notifications via the QuickShell IPC path. Gotcha #6.

---

# 13. Sources

### PySide6 + QML
- https://doc.qt.io/qt-6/qquickpainteditem.html — `paint()` render-thread affinity; `update()` coalescing.
- https://doc.qt.io/qt-6/qqmlengine.html — `setObjectOwnership`, CppOwnership vs JavaScriptOwnership.
- https://doc.qt.io/qtforpython-6/PySide6/QtQml/QmlElement.html — `@QmlElement` + `QML_IMPORT_NAME` registration.
- https://doc.qt.io/qtforpython-6/tutorials/qmlintegration/qmlintegration.html — canonical Python↔QML bridging.
- https://doc.qt.io/qt-6/qtqml-syntax-propertybinding.html — binding semantics; NOTIFY drives re-evaluation.
- https://www.kdab.com/qml-engine-internals-part-2-bindings/ — why non-bindable function calls stale.
- https://www.qt.io/blog/compiling-qml-to-c-fixing-unqualfied-access — unqualified access & QML→C++ compiler.
- https://doc.qt.io/qtforpython-6/PySide6/QtCore/QAbstractListModel.html — `roleNames()`, `beginInsertRows`/`endInsertRows`.
- https://wiki.qt.io/PySide_Shiboken_Object_Ownership — wrapper vs C++ lifecycle.
- https://doc.qt.io/qtforpython-6/tools/pyside-qmllint.html — qmllint with PySide6 type info.

### QML performance
- https://doc.qt.io/qt-6/qtquick-visualcanvas-scenegraph.html — batching, sync phase, render thread.
- https://doc.qt.io/qt-6/qml-qtquick-repeater.html vs https://doc.qt.io/qt-6/qml-qtquick-listview.html — Repeater vs ListView recycling.
- https://www.kdab.com/10-tips-to-make-your-qml-code-faster-and-more-maintainable/ — delegate slimness, avoid `var`.
- https://www.qt.io/blog/whats-new-in-qml-tooling-for-qt-6.11-part-2 — recent qmllint warnings.

### Qt threading
- https://doc.qt.io/qt-6/threads-qobject.html — QObject affinity, connection types, thread-safety.
- https://doc.qt.io/qt-6/qthread.html — worker-object pattern, subclass warnings, quit/wait lifecycle.
- https://doc.qt.io/qt-6/qt.html — `Qt::ConnectionType` enum semantics.
- https://doc.qt.io/qt-6/qmetaobject.html#invokeMethod — cross-thread invocation.
- https://woboq.com/blog/how-qt-signals-slots-work-part3-queuedconnection.html — QueuedConnection event posting deep-dive.
- https://forum.qt.io/topic/160665/pyside6-slot-executed-in-signal-s-thread — @Slot()-on-receiver motivation.

### Python
- https://docs.python.org/3/whatsnew/3.12.html — PEP 695 type syntax, `@override`.
- https://docs.python.org/3/whatsnew/3.14.html — tuple-of-primitives GC tracking (gotcha #10).
- https://peps.python.org/pep-0695/ — generic syntax + `type` aliases.
- https://peps.python.org/pep-0698/ — `@override`.
- https://peps.python.org/pep-0654/ — `ExceptionGroup` (Python 3.11+; no active rules reference this yet, listed for completeness).
- https://docs.python.org/3/library/gc.html — `freeze`, `disable`, generational thresholds.
- https://docs.python.org/3/library/faulthandler.html — segfault traceback capture.
- https://docs.astral.sh/ruff/ — rule coverage vs black/flake8/isort/pylint.
- https://microsoft.github.io/pyright/ — strict mode, PySide6 stub compatibility.

### pynvim + NeoVim
- https://neovim.io/doc/user/ui.txt — grid protocol, ext_* options, flush semantics.
- https://neovim.io/doc/user/api.txt — `nvim_ui_attach`, `nvim_get_keymap`, `nvim_set_client_info`.
- https://neovim.io/doc/user/lua.txt — `vim.schedule`, `vim.keymap.set`, `vim.cmd.normal`, `vim.on_key`.
- https://pynvim.readthedocs.io/en/latest/usage/python-plugin-api.html — threading, `async_call`, subscribe.
- https://github.com/neovim/neovim/blob/master/src/nvim/api/ui.c — authoritative redraw event arg counts.
- https://github.com/folke/which-key.nvim/tree/main/lua/which-key/plugins/presets.lua — preset catalog we flatten.
- https://github.com/neovide/neovide/tree/main/src/renderer/cursor_renderer — reference frontend; scroll/cursor spring patterns (gotchas #11, #12). Note: `src/bridge` is the msgpack-RPC layer; the spring algorithms live in `src/renderer/cursor_renderer/mod.rs`.
- https://github.com/Kampfkarren/selene — Lua linter with Neovim std.
- https://github.com/nvim-lua/plenary.nvim — busted test runner pattern.

### Testing
- https://pytest-qt.readthedocs.io/en/latest/signals.html — `waitSignal`, timeout semantics.
- https://pytest-qt.readthedocs.io/en/latest/intro.html — `qtbot` fixture, qApp lifecycle.
- https://ilmanzo.github.io/post/testing_pyside_gui_applications/ — offscreen platform plugin for headless CI.

### This project
- `CLAUDE.md` gotchas #1–#19 — the concrete incidents that motivate most P0 rules here.
