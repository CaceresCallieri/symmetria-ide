// SPDX-License-Identifier: GPL-3.0-or-later
//
// `SymmetriaHostKeyHandler` — takes the place of the PROCESS-WIDE key event
// handler that QWaylandCompositorPrivate installs behind our back.
//
// WHY THIS EXISTS. QWaylandCompositorPrivate's CONSTRUCTOR calls
// `QWindowSystemInterfacePrivate::installWindowSystemEventHandler` with a
// handler that intercepts EVERY key event in the process — the terminals, the
// editor, the agent panes, the IDE chords, not just the browser — and rewrites
// each one from the NESTED seat's xkb state (qtwayland 6.11.1,
// qwaylandcompositor.cpp, `QtWayland::WindowSystemEventHandler`):
//
//     ke->key = qtkey;  ke->modifiers = modifiers;  ke->unicode = text;
//
// That state is DEAD-RECKONED: it moves only when a press or a release is
// delivered to this application. The host compositor's authoritative
// `wl_keyboard.modifiers` — what every other app on the system uses, and what
// Hyprland resends on every focus enter — is thrown away. Nothing ever
// reconciles the two.
//
// Two user-visible bugs follow, and BOTH were first reported as something else:
//
//   * WRONG LAYOUT. QWaylandKeymap defaults to xkb's default, `us`, so on a
//     latam session the whole IDE silently typed US the moment the browser
//     pane loaded. Worked around separately by pinning the nested seat's
//     keymap to the host's — BrowserPane.qml + src/symmetria_ide/keyboard_layout.py.
//
//   * STUCK MODIFIERS (what this class fixes). Hold Shift, change Hyprland
//     workspace while holding it — with kanata home-row mods that is the
//     ordinary way to move — and release Shift while the IDE is unfocused. The
//     release never reaches this process, so the nested state keeps Shift
//     depressed FOREVER and every later key arrives with a phantom
//     Qt::ShiftModifier: Ctrl+U fires Ctrl+Shift+U (the VPS toggle), Ctrl+V
//     fires Ctrl+Shift+V. Qt's own safety net — `resetKeyboardState()` on
//     Qt::ApplicationInactive, qwaylandcompositor.cpp — demonstrably does not
//     cover it: the reported repair is a fresh Shift press+release, while
//     merely switching windows (which does change focus) does NOT clear it.
//
// WHAT THIS DOES. It displaces Qt's handler and keeps only the half that is
// genuinely needed — mirroring presses and releases into the nested seat, so
// Chrome still gets `wl_keyboard.modifiers` — while leaving the host's own
// QKeyEvent exactly as the Wayland CLIENT plugin translated it. The host app
// goes back to the host compositor's state, which cannot go stale because
// Hyprland resends it on every change and on every focus enter.
//
// Nested clients lose nothing: Wayland delivers keys as scan codes plus a
// modifier mask and each client applies its own keymap, so the rewritten
// `ke->key`/`unicode` were never part of what Chrome sees. Chrome's own
// modifier state also gets BETTER, not worse — `QWaylandSeat::sendFullKeyEvent`
// already calls `checkAndRepairModifierState(event)` on every non-modifier
// press, and that repair reads `event->modifiers()`, which is now truthful.
//
// It also closes a latent drop: Qt's handler `return`s early when the
// compositor has no default seat yet and never delivers the event at all, so
// keys pressed before seat initialisation are swallowed. This one always
// delivers, seat or no seat.
//
// PRIVATE API. `QWindowSystemEventHandler` and `QWaylandKeyboardPrivate` are
// both private Qt API with no source or binary compatibility promise — the
// same trade already accepted in symmetriashellsurfaceitem.cpp, and with the
// same intended failure mode: a Qt update that moves them is a LOUD compile
// error, not a silent behaviour change, because the PKGBUILD declares the Qt
// deps and a Qt bump flags this package for rebuild.
//
// REMOVAL CRITERION: when the nested compositor moves OUT of the IDE's process
// (the true fix recorded in keyboard_layout.py), Qt's handler stops seeing the
// host's keys at all and this class becomes pointless. Delete it then, together
// with the keymap pin.

#pragma once

#include <QtGui/qpa/qwindowsysteminterface_p.h>

QT_BEGIN_NAMESPACE
class QWaylandCompositor;
class QWaylandKeyboard;
QT_END_NAMESPACE

class SymmetriaHostKeyHandler : public QWindowSystemEventHandler
{
public:
    // Displaces whatever handler is currently installed (in practice Qt's, put
    // there by the QWaylandCompositor base constructor) and installs itself.
    explicit SymmetriaHostKeyHandler(QWaylandCompositor *compositor);
    ~SymmetriaHostKeyHandler() override;

    bool sendEvent(QWindowSystemInterfacePrivate::WindowSystemEvent *event) override;

private:
    QWaylandKeyboard *nestedKeyboard() const;

    QWaylandCompositor *m_compositor = nullptr;
    // Qt's handler, kept so teardown leaves the process as it was found. Owned
    // by QWaylandCompositorPrivate, never by us.
    QWindowSystemEventHandler *m_displaced = nullptr;
};
