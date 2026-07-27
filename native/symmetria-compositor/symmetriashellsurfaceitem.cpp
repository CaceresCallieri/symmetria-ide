// SPDX-License-Identifier: GPL-3.0-or-later
// See symmetriashellsurfaceitem.h for why this class exists.

#include "symmetriashellsurfaceitem.h"

#include <QtGui/QWheelEvent>

namespace {

// The divisor inside QWaylandPointer::sendMouseWheelEvent. Anything that is
// not a whole multiple of this is truncated away there, so this is the grain
// we have to quantise to before handing the event over.
constexpr int kWaylandAngleStep = 12;

// Largest multiple of the step that `value` covers, keeping the sign. Integer
// division truncates TOWARD ZERO in C++, which is what we want in both
// directions: it never overshoots, so the carry never changes sign.
int quantise(int value)
{
    return (value / kWaylandAngleStep) * kWaylandAngleStep;
}

} // namespace

SymmetriaShellSurfaceItem::SymmetriaShellSurfaceItem(QQuickItem *parent)
    : QWaylandQuickShellSurfaceItem(parent)
{
}

void SymmetriaShellSurfaceItem::wheelEvent(QWheelEvent *event)
{
    m_carry += event->angleDelta();

    const QPoint step(quantise(m_carry.x()), quantise(m_carry.y()));
    if (step.isNull()) {
        // Below the grain in both axes. Accepted rather than ignored: the
        // scroll is not being refused, it is being SAVED — passing it on would
        // let an ancestor act on motion the client is also about to receive.
        event->accept();
        return;
    }
    m_carry -= step;

    // Re-quantised copy handed to the base class, so Qt still owns the
    // input-region test, the seat lookup and the actual send. Reimplementing
    // those here would create a second send path to keep in sync.
    QWheelEvent forwarded(event->position(), event->globalPosition(),
                          event->pixelDelta(), step, event->buttons(),
                          event->modifiers(), event->phase(), event->inverted(),
                          Qt::MouseEventSynthesizedByApplication,
                          event->pointingDevice());
    QWaylandQuickShellSurfaceItem::wheelEvent(&forwarded);
    event->accept();
}
