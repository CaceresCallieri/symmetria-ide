---
name: nested-compositor-hijacks-host-keymap
description: "A nested QWaylandCompositor makes the WHOLE host app translate keys with ITS seat keymap — Qt's default US — so the browser pane silently switched the IDE to a US keyboard"
metadata: 
  node_type: memory
  type: reference
  originSessionId: cbb70dc8-2141-4107-8ea7-a38b45169bf0
  modified: 2026-08-05T00:02:26.256Z
---

Constructing a `QWaylandCompositor` **anywhere in the process** makes the HOST
window stop translating keys with the keymap the real compositor sent it and
start using the NESTED seat's keymap. `QWaylandKeymap` defaults to the xkb
defaults — layout `us` — so on a `latam` session the entire IDE (terminals,
agent panes, editor, chords) silently switched to a US keyboard the moment
`BrowserPane` loaded.

This reads as everything except what it is. The user's report was "the keyboard
layout broke and I can't do practically anything"; the natural suspects are
Hyprland's `kb_layout`, kanata, xkeyboard-config, or the qmltermwidget fork's
keytab. All four were measured and all four were innocent.

## How it was found (2026-08-04)

The discriminating fact: **old IDE windows were fine, new ones broken.** Env
was byte-identical between an old and a new instance, so the variable was the
promoted code, not the environment. `git reflog` in the stable worktree dated
the promotion at 20:14:08 — the exact minute the broken instances start. That
promotion carried the agentic browser (merged to dev 2026-07-27, after the
previous 2026-07-23 promotion), i.e. the first time stable ever constructed a
nested compositor.

Independent corroboration, cheap and worth repeating: `/run/user/$UID/` holds a
`symmetria-browser-<pid>` socket **only** for the post-promotion instances.

## The measurement

A minimal PySide6 probe — a plain `QQuickWindow` printing every `QKeyEvent`'s
`text` and `nativeScanCode` — with `wtype` injecting a fixed string. `wtype`
uploads its own keymap, so a client that honours keymap updates reproduces the
injected string exactly, and one that does not reveals which keymap it fell
back to. X11 scancode 47 is the discriminator: US `;` vs latam `ñ`.

    no compositor .................. injected string reproduced exactly
    + WaylandCompositor (empty) .... SC47=';'  -> us
    + XdgShell ..................... SC47=';'  -> us   (shell is irrelevant)
    + keymap forced to "latam" ..... SC47='ñ', SC21='¿', SC49='|'  -> latam

An **empty** compositor — no shell, no output, no client — is already enough.
A deferred variant (plain window, compositor constructed by a timer 3s later,
keys injected before and after) translated correctly right up to the instant of
construction and incorrectly one instant after: **lazy-loading the pane is not
a fix.**

Confirmed against the live daily driver rather than only the probe: reading the
running IDE's nested socket with
`WAYLAND_DISPLAY=symmetria-browser-<pid> xkbcli interactive-wayland --verbose`
printed `Compiling xkb_symbols "pc_us_inet(evdev)"` while the host session
printed `pc_latam_inet(evdev)`. That one-liner is the fastest way to re-check
this at any time.

## The fix

[`src/symmetria_ide/keyboard_layout.py`](../../../../src/symmetria_ide/keyboard_layout.py)
resolves the host's RMLVO tuple (Hyprland session > `XKB_DEFAULT_*` > empty),
`app.py` exposes it as the `hostKeymap` context property, and
`qml/browser/BrowserPane.qml` assigns it onto `defaultSeat.keymap` in
`Component.onCompleted` — imperatively, because `defaultSeat` is a read-only
object property and `defaultSeat.keymap.layout: x` is not a legal binding
target.

It pins rather than repairs: the host still ignores keymap CHANGES, which only
matters for virtual-keyboard tools that upload their own keymap. The compiled
result keeps a stray unreachable second `us` group (`pc_latam_us_2_inet`) that
is Qt's doing, not the assignment style's. A true fix means moving the nested
compositor out of the IDE's process.

Related: [nested-compositor-pointer-input](./nested_compositor_pointer_input.md),
[nested-compositor-output-mode](./nested_compositor_output_mode.md) — same
pattern, a nested compositor needing something Qt does not do by default.
