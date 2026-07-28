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
// WHAT IT DOES, in two parts — reverting to the stock item breaks scrolling
// through EITHER of them, so both have to be understood together:
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
//
// ⚠ Popups get NEITHER. Qt creates those items itself in C++
// (`maybeCreateAutoPopup`) and there is no hook to substitute a subclass, so
// scrolling INSIDE a long Chrome dropdown hits the stock arithmetic AND never
// gets a frame. Fixing that means creating popup items from QML, which means
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

private:
    // Sub-threshold scroll carried into the next event. Per-item, because two
    // browser windows scroll independently.
    QPoint m_carry;
};
