// SPDX-License-Identifier: GPL-3.0-or-later
//
// `SymmetriaShellSurfaceItem` — a ShellSurfaceItem whose wheel events survive
// the trip to the client.
//
// WHY THIS EXISTS. `QWaylandPointer::sendMouseWheelEvent` ends in:
//
//     d->send_axis(resource->handle, time, axis, wl_fixed_from_int(-delta / 12));
//
// `delta` is the Qt `angleDelta` and the division is TRUNCATING INTEGER
// division, so every wheel event smaller than 12 becomes a zero-valued axis
// event — sent, accepted, and doing nothing.
//
// That is not an edge case on modern hardware. A classic detented wheel
// reports 120 per notch and survives; a HIGH-RESOLUTION wheel reports the same
// notch as a stream of small fragments, and they land under the threshold and
// vanish. Measured on the machine this was found on: the mouse advertises
// `REL_WHEEL_HI_RES` and NOT `REL_WHEEL` — it cannot emit a 120-unit step at
// all — and the touchpad reports no wheel axis whatsoever. Both scroll
// entirely in fragments, so pages did not scroll at all.
//
// The symptom points away from the cause, which is why this is worth a class:
// dragging a page's scrollbar works perfectly, because press/move/release never
// touch this arithmetic. That makes it look like a focus or hover problem.
//
// WHAT IT DOES, in three parts — reverting to the stock item breaks scrolling
// through either of the first two, so those have to be understood together:
//
//  1. Accumulates `angleDelta` and forwards only whole multiples of 12,
//     carrying the remainder to the next event, so fragments add up instead of
//     being discarded. The forwarding hands a re-quantised event to the BASE
//     implementation rather than reimplementing the send — Qt keeps ownership
//     of the input-region and focus checks, and there is one send path.
//  2. Sends `wl_pointer.frame` after each axis. Chromium NEVER dispatches a
//     scroll on the axis event itself; it only buffers, and the single call
//     site that flushes the buffer lives in its frame handler. Without this
//     the browser does not scroll at all, however correct the axis values are.
//     The full argument, including why it is a deliberate protocol deviation,
//     is on `sendPointerFrame` in the .cpp.
//  3. Accepts HOVER events, for itself and for the popup items Qt builds under
//     it. `QWaylandQuickItem::hoverEnterEvent` / `hoverMoveEvent` are the only
//     callers of `sendMouseMoveEvent` — the call that gives the seat's pointer
//     a focused surface and a position — and NOTHING in Qt turns hover on:
//     verified against 6.8 source that neither `QWaylandQuickItem` nor
//     `QWaylandQuickShellSurfaceItem` ever calls `setAcceptHoverEvents`. So a
//     stock item receives pointer motion only while a button is held, and the
//     client is told where the pointer is only when something is clicked.
//
// ⚠ (3) REPLACES A DEAD QML ATTEMPT — do not restore it. `BrowserPane.qml`
// used to walk the item tree assigning `item.hoverEnabled = true`, but
// `hoverEnabled` is not a property of `QQuickItem`, `QWaylandQuickItem` or
// `QWaylandQuickShellSurfaceItem` (in Qt 6.11 that Q_PROPERTY exists only on
// `MouseArea` and three QtQuickTemplates2 types). The assignment threw on its
// first line, which aborted the function before it could recurse or connect its
// signal — so the walk never ran at all, for the surface OR the popups, and its
// only trace was a single "Cannot assign to non-existent property" per session.
// The real API is `setAcceptHoverEvents`, a C++ method with no QML equivalent,
// which is why this belongs here and could never have worked there.
//
// Popups are handled by walking children in C++ because Qt builds them itself
// (`maybeCreateAutoPopup`) as stock items we never get to declare. That walk
// can give them (3) but NOT (1) or (2), which need the subclass: scrolling
// INSIDE a long Chrome dropdown still hits the stock arithmetic and never gets
// a frame. Fixing that means creating popup items from QML, which means
// reimplementing Qt's popup positioning — a worse trade for a rarer case.

#pragma once

#include <QtCore/QPoint>
#include <QtQml/qqmlregistration.h>
#include <QtWaylandCompositor/QWaylandQuickShellSurfaceItem>

QT_BEGIN_NAMESPACE
class QWheelEvent;
QT_END_NAMESPACE

class SymmetriaShellSurfaceItem : public QWaylandQuickShellSurfaceItem
{
    Q_OBJECT
    QML_NAMED_ELEMENT(SymmetriaShellSurfaceItem)

public:
    explicit SymmetriaShellSurfaceItem(QQuickItem *parent = nullptr);

protected:
    void wheelEvent(QWheelEvent *event) override;

private Q_SLOTS:
    // Re-sweeps the item that emitted `childrenChanged`. A member slot, not a
    // lambda, so the connection can be `UniqueConnection` — see the .cpp.
    void onChildrenChanged();

private:
    // Turns hover on for `item` and everything under it, and keeps doing so as
    // Qt adds children — a popup can parent a further popup (a submenu inside a
    // menu), and `childrenChanged` only reports what arrives AFTER the connect,
    // so each new item is swept immediately as well as watched.
    void enableHoverTree(QQuickItem *item);

    // Sub-threshold scroll carried into the next event. Per-item, because two
    // browser windows scroll independently.
    QPoint m_carry;
};
