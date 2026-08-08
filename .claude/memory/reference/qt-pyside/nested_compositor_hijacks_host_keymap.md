---
name: nested-compositor-hijacks-host-keymap
description: "A nested QWaylandCompositor installs a PROCESS-WIDE key handler that rewrites every key from its own dead-reckoned xkb state — causing both the US-keyboard hijack and phantom stuck modifiers"
metadata:
  node_type: memory
  type: reference
  originSessionId: cbb70dc8-2141-4107-8ea7-a38b45169bf0
  modified: 2026-08-08T09:36:15.890Z
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

## The mechanism (found 2026-08-08, from the qtwayland source)

The 2026-08-04 entry above stopped at *what* happens. The *how* is one function,
and it explains a second bug as well.

`QWaylandCompositorPrivate`'s **constructor** calls
`QWindowSystemInterfacePrivate::installWindowSystemEventHandler` with
`QtWayland::WindowSystemEventHandler` (qtwayland 6.11.1,
`src/compositor/compositor_api/qwaylandcompositor.cpp`). That slot is a single
pointer with **first-wins** semantics — no chain, no second handler — and the
handler sees **every key event in the process**, then rewrites it:

    ke->key = qtkey;  ke->modifiers = modifiers;  ke->unicode = text;
    ke->nativeVirtualKey = sym;  ke->nativeModifiers = keyb->xkbModsMask();

all computed from `keyb->xkbState()`, the **nested seat's** state. That state is
**dead-reckoned**: `updateModifierState(code, state)` moves it only when a press
or release is delivered to this app. The host compositor's authoritative
`wl_keyboard.modifiers` — which the Wayland *client* plugin keeps correct, and
which Hyprland resends on every focus `enter` — is discarded. Nothing
reconciles them. This is why no other app on the system can desync.

Two consequences, both reported as system-wide faults:

1. **Wrong layout** — the 2026-08-04 report above.
2. **Phantom stuck modifiers** (reported 2026-08-08). Hold Shift, change
   Hyprland workspace while holding it — with kanata home-row mods that is the
   ordinary way to move — and release it while the IDE is unfocused. The
   release never reaches this process, so Shift stays depressed in the nested
   state and every later key carries a phantom `Qt::ShiftModifier`: `Ctrl+U`
   fires the `Ctrl+Shift+U` VPS toggle, `Ctrl+V` fires `Ctrl+Shift+V`. Only
   this app, because only this app has a nested compositor.

The discriminating observation was the user's own repair: **a fresh Shift
press+release clears it, while merely switching windows does not.** A focus
change is what would trigger Qt's own safety net
(`QWaylandCompositor::applicationStateChanged` → `resetKeyboardState()` on
`Qt::ApplicationInactive`), so that net demonstrably does not cover this path;
a balanced release is what a dead-reckoned state needs. Why the net misses is
still unknown and deliberately not relied on.

Also latent in Qt's handler: it `return`s early when the compositor has no
default seat and **never delivers the event**, so keys pressed before seat
initialisation are swallowed.

## The real fix (2026-08-08)

[`native/symmetria-compositor/symmetriahostkeys.h`](../../../../native/symmetria-compositor/symmetriahostkeys.h)
— `SymmetriaHostKeyHandler`, constructed by `SymmetriaCompositor`'s constructor
(the only moment Qt's handler is already installed and we still run). It
displaces Qt's handler and keeps only the necessary half: mirror presses and
releases into the nested seat, so Chrome still receives `wl_keyboard.modifiers`,
and leave the host's `QKeyEvent` exactly as the client plugin translated it.
Nested clients lose nothing — Wayland delivers scan codes plus a modifier mask
and each client applies its own keymap, so the rewritten `key`/`unicode` were
never part of what Chrome sees. Chrome's own state gets better: Qt's
`checkAndRepairModifierState` (called from `QWaylandSeat::sendFullKeyEvent`)
reads `event->modifiers()`, which is now truthful.

## The keymap pin (2026-08-04) — still required, now narrower

[`src/symmetria_ide/keyboard_layout.py`](../../../../src/symmetria_ide/keyboard_layout.py)
resolves the host's RMLVO tuple — rules/model/layout/variant/options —
(`SYMMETRIA_IDE_KEYMAP_*` override > Hyprland session > `XKB_DEFAULT_*` >
empty), `app.py` exposes it as the `hostKeymap` context property, and
`qml/browser/BrowserPane.qml` takes it as a `required property` and assigns it
onto `defaultSeat.keymap` in `Component.onCompleted` — imperatively, because
`defaultSeat` is a read-only object property and `defaultSeat.keymap.layout: x`
is not a legal binding target.

For a multi-group config (`kb_layout = us,latam`) the layout list is rotated so
the group active at startup lands first, since xkb has no active-group setter
and order is the only lever.

The compiled result keeps a stray unreachable second `us` group
(`pc_latam_us_2_inet`) that is Qt's doing, not the assignment style's — that
name is a PASS, not a regression.

⚠ Its SCOPE changed on 2026-08-08 and the two halves are easy to confuse. It is
no longer what keeps the IDE typing correctly — `SymmetriaHostKeyHandler` cut
that dependency off at the source. What it still does, and must keep doing, is
give **Chrome** the user's layout, because the nested seat's keymap is the one
Chrome compiles. Keep both; neither substitutes for the other.

The whole apparatus disappears when the nested compositor moves OUT of the
IDE's process — Qt's handler would then never see the host's keys, and the
nested seat's keymap would be that other process's business.

Related: [nested-compositor-pointer-input](./nested_compositor_pointer_input.md),
[nested-compositor-output-mode](./nested_compositor_output_mode.md) — same
pattern, a nested compositor needing something Qt does not do by default.
