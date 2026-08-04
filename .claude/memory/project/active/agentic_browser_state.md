---
name: agentic-browser-state
description: "Agentic browser = real Chrome in a nested Wayland compositor, shipped on dev; NOT yet in stable — promotion needs the new symmetria-compositor package"
metadata:
  node_type: memory
  type: project
  originSessionId: 1dc16f14-6524-437a-9b81-8d0fde68876c
  modified: 2026-08-04T20:03:22.376Z
---

# Agentic browser: where the work stands

Architecture, invariants and gotchas live in CLAUDE.md "The agentic browser" and
in `chrome_host.py` / `cdp_client.py` / `native/symmetria-compositor/`. This file
carries only what those don't: the promotion state and the decisions.

*(Renamed from `chrome_external_browser.md` on 2026-08-04 — the browser stopped
being external the day that file was written.)*

**Two backends were retired to get here, both on 2026-07-27.** First the embedded
QtWebEngine pool (`new_page` unsupported, screenshots stalling off-workspace, no
extensions, no real logins). Then, the same day, a brief intermediate that ran
real Chrome as an **external window pinned by a Hyprland rule**. Today Chrome is
an ordinary client of a nested Wayland compositor the IDE hosts, so it renders
inside the IDE window and `hyprctl clients` does not list it at all.

## Promotion state — the reason this is still `active`

**Shipped on `dev`; NOT in `main`.** Verified 2026-08-04. Promoting is not just a
merge — two steps live outside git, and neither fails loudly:

1. **`symmetria-compositor` is a brand-new pacman package.** `native/` does not
   exist in `main` at all. Build it with `makepkg -si` from
   `native/symmetria-compositor/`, from the COMMITTED tree. Without it the
   browser pane does not load at all (its Loader contains the failure, so the
   rest of the IDE is fine — which is exactly why it is easy to miss).
2. **`google-chrome` (AUR) becomes a runtime dep.** Degrades cleanly when absent
   (the browser tools return `chrome-not-installed`), so its absence also fails
   quietly.

Nothing else: the sidecar and the qmltermwidget fork are untouched by this work,
so no `npm run build` and no second `makepkg`. `qt6-webengine` stops being needed.

## User decisions — do not relitigate

- **Full Chrome UI, not `--app=`.** The browser doubles as a surface for showing
  things to other people, so tabs and the omnibox are wanted.
- **Profile: template + clone.** One shared login across projects is
  architecturally impossible — Chrome is a singleton per `--user-data-dir`, so a
  shared profile means one process and one nested compositor. Seeding each
  project profile from a `_template` was the accepted answer.
- **⚠ REVERSED: "notify, don't yank".** This file used to record that
  `Ctrl+Shift+B` and the chip globe must only toast, never focus. That was an
  artifact of the pinned-window backend, where raising the browser meant a
  workspace switch nobody asked for. With the browser inside the IDE there is no
  workspace to switch, and `Ctrl+Shift+B` navigates like any other surface chord.
  **Do not restore the notify-only behaviour** — CLAUDE.md says so too.

## The open items from the spike — all closed but one

1. ~~Clipboard isolated both ways~~ — **bridged**, and verified end to end by
   round-tripping sentinel strings. The two directions have different focus
   requirements; details in
   [nested-compositor-clipboard](../../reference/qt-pyside/nested_compositor_clipboard.md).
2. **No `zwp_linux_dmabuf_v1`** — still true. Chrome falls back to `wl_shm` (CPU
   buffer copies per frame). Rendered fine in practice; still unmeasured under
   video/animation load. The one genuinely open item.
3. ~~Fractional scaling~~ — solved, but not the way this file guessed: the
   `wl_output` scale is an integer by protocol, so the host's fractional DPR is
   rounded UP and Chrome oversamples. See
   [nested-compositor-output-mode](../../reference/qt-pyside/nested_compositor_output_mode.md).
4. ~~Chord precedence unverified~~ — verified, and it is **structural, not luck**:
   `QWaylandQuickItem` never accepts `ShortcutOverride`, so an
   `ApplicationShortcut` always wins. Stronger than the QMLTermWidget case.

A fifth item nobody predicted turned out to matter more than all of them: the
nested client **stalls forever** when the host stops producing frames, which
needed a watchdog —
[nested-compositor-frame-starvation](../../reference/qt-pyside/nested_compositor_frame_starvation.md).

## Still-open gap

Windows an agent creates directly through chrome-devtools-mcp are unattributed
(needs an IDE-side `Target.targetCreated` monitor). Related and load-bearing: a
registry "window" is a CDP **page target** while the pane multiplexes Wayland
**toplevels**, and `new_page` opens a TAB — so the two counts legitimately
diverge and must never be joined.
