// SPDX-License-Identifier: GPL-3.0-or-later
// See symmetriashellsurfaceitem.h for why this class exists.

#include "symmetriashellsurfaceitem.h"

#include "symmetriatrace.h"

#include <QtGui/QWheelEvent>
#include <QtWaylandCompositor/QWaylandCompositor>
#include <QtWaylandCompositor/QWaylandPointer>
#include <QtWaylandCompositor/QWaylandSeat>
#include <QtWaylandCompositor/QWaylandSurface>
#include <QtWaylandCompositor/QWaylandView>

// Private, deliberately — `send_frame` exists nowhere else. See sendPointerFrame.
#include <QtCore/private/qobject_p.h>
#include <QtWaylandCompositor/private/qwaylandpointer_p.h>

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

// Sends `wl_pointer.frame` to the client currently under the pointer.
//
// WHY THIS IS NEEDED AT ALL. Chromium never acts on a scroll when the axis
// event arrives — `WaylandEventSource::OnPointerAxisEvent` only ACCUMULATES
// into `pointer_scroll_data_`, and the single call site that dispatches it,
// `ProcessPointerScrollData()`, lives inside `OnPointerFrameEvent()`. There is
// no timeout and no other trigger. Qt's compositor never sends a frame, so
// every scroll piled into a buffer that was never flushed: the events arrived,
// were forwarded correctly, and did nothing. Buttons and motion are unaffected
// because Chromium dispatches those immediately unless a feature flag says
// otherwise, which is why clicking and dragging a scrollbar always worked.
//
// ⚠ THIS IS A DELIBERATE PROTOCOL DEVIATION. `frame` is a wl_pointer VERSION 5
// event and Qt hardcodes its seat global at version 4
// (`QWaylandSeat::initialize`: `d->init(d->compositor->display(), 4)`), so we
// are sending an event newer than the version negotiated with the client. It
// is received regardless: Chromium registers `.frame = &OnFrame` in its
// listener unconditionally, and libwayland-client demarshals by opcode against
// the full interface table without consulting the bound version. Verified in
// both sources, and the negotiated version was read off a live WAYLAND_DEBUG
// trace.
//
// What makes it defensible rather than reckless is that this compositor has
// exactly ONE client: the Chrome process the IDE spawns itself, on a socket
// named after the IDE's pid. A client with a listener struct predating
// `frame` (libwayland < 1.10) would read past the end of it — but no such
// client can reach this socket.
//
// The protocol-correct alternative is worse, not better: advertising version 5
// makes frames MANDATORY for every pointer event group, so motion, buttons,
// enter and leave would all have to emit them too, and the version bump itself
// means reimplementing `QWaylandSeat::initialize()` against private internals.
// That is strictly more private API and strictly more to break. Remove this
// the day Qt's compositor advertises 5 and sends its own frames — that is the
// real upstream fix.
void sendPointerFrame(QWaylandSeat *seat)
{
    QWaylandPointer *pointer = seat != nullptr ? seat->pointer() : nullptr;
    if (pointer == nullptr)
        return;
    const QWaylandView *focus = pointer->mouseFocus();
    QWaylandSurface *surface = focus != nullptr ? focus->surface() : nullptr;
    if (surface == nullptr)
        return;

    auto *priv = static_cast<QWaylandPointerPrivate *>(QObjectPrivate::get(pointer));
    // Scoped to the focused client rather than broadcast, mirroring what
    // Qt's own axis send does — a frame is a statement about one client's
    // event sequence, not a global tick.
    const auto resources = priv->resourceMap().values(surface->waylandClient());
    for (auto *resource : resources)
        priv->send_frame(resource->handle);
}

} // namespace

SymmetriaShellSurfaceItem::SymmetriaShellSurfaceItem(QQuickItem *parent)
    : QWaylandQuickShellSurfaceItem(parent)
{
}

void SymmetriaShellSurfaceItem::wheelEvent(QWheelEvent *event)
{
    m_carry += event->angleDelta();

    // The one place that can distinguish "the wheel never arrived" from "it
    // arrived and was thrown away", which every other symptom of this confuses.
    // Enable with SYMMETRIA_COMPOSITOR_DEBUG=1; see symmetriatrace.h for why
    // this does not go through Qt logging.
    symmetria::trace(
        "wheel angle=(%d,%d) pixel=(%d,%d) phase=%d carry=(%d,%d) surface=%s",
        event->angleDelta().x(), event->angleDelta().y(),
        event->pixelDelta().x(), event->pixelDelta().y(), int(event->phase()),
        m_carry.x(), m_carry.y(), surface() != nullptr ? "yes" : "NONE");

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

    // The base call above sent the axis. Without the frame that follows it,
    // Chromium buffers the scroll and never dispatches it — see
    // sendPointerFrame. Sent only when an axis actually went out, so an
    // under-threshold event does not emit an empty sequence.
    if (QWaylandCompositor *comp = compositor())
        sendPointerFrame(comp->seatFor(event));

    event->accept();
}
