// SPDX-License-Identifier: GPL-3.0-or-later
// See symmetriahostkeys.h for why this class exists at all.

#include "symmetriahostkeys.h"

#include "symmetriatrace.h"

#include <QtWaylandCompositor/QWaylandCompositor>
#include <QtWaylandCompositor/QWaylandKeyboard>
#include <QtWaylandCompositor/QWaylandSeat>
#include <QtWaylandCompositor/private/qwaylandkeyboard_p.h>

namespace {
using symmetria::trace;
} // namespace

SymmetriaHostKeyHandler::SymmetriaHostKeyHandler(QWaylandCompositor *compositor)
    : m_compositor(compositor)
{
    // `installWindowSystemEventHandler` is FIRST-WINS — it no-ops when a
    // handler is already set (qwindowsysteminterface.cpp). Qt's is already
    // there by now, because QWaylandCompositorPrivate installs it from its own
    // constructor and that runs before any subclass body. Displacing it is the
    // only way in; there is no chaining and no second slot.
    m_displaced = QWindowSystemInterfacePrivate::eventHandler;
    if (m_displaced != nullptr)
        QWindowSystemInterfacePrivate::removeWindowSystemEventhandler(m_displaced);
    QWindowSystemInterfacePrivate::installWindowSystemEventHandler(this);

    trace("host key handler installed (displaced Qt's: %s)",
          m_displaced != nullptr ? "yes" : "NO — none was installed");
}

SymmetriaHostKeyHandler::~SymmetriaHostKeyHandler()
{
    QWindowSystemInterfacePrivate::removeWindowSystemEventhandler(this);
    // Hand the slot back, so a compositor torn down mid-session leaves the
    // process as it found it. Safe against our own base destructor, which runs
    // after this body and only clears the slot while it still points at `this`.
    if (m_displaced != nullptr)
        QWindowSystemInterfacePrivate::installWindowSystemEventHandler(m_displaced);
}

QWaylandKeyboard *SymmetriaHostKeyHandler::nestedKeyboard() const
{
    if (m_compositor == nullptr)
        return nullptr;
    // Resolved per event rather than cached: seats are created in
    // QWaylandCompositor::create(), long after this handler is installed, and
    // an application may replace the default seat at any point.
    QWaylandSeat *seat = m_compositor->defaultSeat();
    return seat != nullptr ? seat->keyboard() : nullptr;
}

bool SymmetriaHostKeyHandler::sendEvent(
    QWindowSystemInterfacePrivate::WindowSystemEvent *event)
{
    if (event->type != QWindowSystemInterfacePrivate::Key)
        return QWindowSystemEventHandler::sendEvent(event);

    auto *keyEvent =
        static_cast<QWindowSystemInterfacePrivate::KeyEvent *>(event);

    // Deliberately NOT touched: key, modifiers, unicode, nativeVirtualKey,
    // nativeModifiers. Qt's handler overwrites all five from the nested seat's
    // dead-reckoned xkb state; leaving them alone is the entire fix. See the
    // header.
    QWaylandKeyboard *keyboard = nestedKeyboard();

    // Auto-repeat is skipped for the same reason Qt skips it: a repeat is not a
    // new physical press, so mirroring it would leave the nested seat's pressed
    // key list holding one entry per repeat tick and its modifier state
    // unbalanced.
    uint32_t code = 0;
    if (keyboard != nullptr && !keyEvent->repeat) {
        code = keyEvent->nativeScanCode;
        if (code == 0)
            code = keyboard->keyToScanCode(keyEvent->key);
    }
    const uint32_t state = keyEvent->keyType == QEvent::KeyPress
        ? WL_KEYBOARD_KEY_STATE_PRESSED
        : WL_KEYBOARD_KEY_STATE_RELEASED;

    QWaylandKeyboardPrivate *nested =
        code != 0 ? QWaylandKeyboardPrivate::get(keyboard) : nullptr;

    // Split around delivery exactly as Qt does: the pressed-key bookkeeping
    // happens before, the modifier state after, so a modifier key's own effect
    // is not applied to the event that carried it.
    if (nested != nullptr)
        nested->keyEvent(code, state);

    // `event` may be gone once this returns, which is why `code` and `state`
    // are read out above rather than after.
    const bool delivered = QWindowSystemEventHandler::sendEvent(event);

    if (nested != nullptr) {
        nested->maybeUpdateKeymap();
        nested->updateModifierState(code, state);
    }

    return delivered;
}
