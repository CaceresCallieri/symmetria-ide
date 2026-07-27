// SPDX-License-Identifier: GPL-3.0-or-later
// See symmetriaoutput.h for why this class exists.

#include "symmetriaoutput.h"

#include <QtCore/QtMath>
#include <QtGui/QScreen>
#include <QtGui/QWindow>
#include <QtWaylandCompositor/QWaylandOutputMode>

namespace {

// Refresh rate is in mHz over the wire. 60Hz is only the last resort for an
// output with no window and no prior mode; it affects nothing we rely on
// (clients read it for frame pacing, and ours renders on the host's vsync
// regardless), but a zero would make the mode INVALID and the whole call a
// silent no-op.
constexpr int kFallbackRefreshRateMilliHz = 60000;

} // namespace

void SymmetriaOutput::setModeSize(const QSize &size)
{
    // A pane still being laid out is 0-sized, and `QWaylandOutputMode` rejects
    // that as invalid — so this would otherwise be a warning per startup frame.
    if (size.isEmpty())
        return;

    const QWaylandOutputMode existing = currentMode();
    // The no-op that keeps the mode list from growing (see the header): callers
    // are expected to fire this on every geometry change, and most of those
    // resolve to the same size.
    if (existing.isValid() && existing.size() == size)
        return;

    int refreshRate = existing.isValid() ? existing.refreshRate() : 0;
    if (refreshRate <= 0) {
        const QWindow *hostWindow = window();
        const QScreen *screen =
            hostWindow != nullptr ? hostWindow->screen() : nullptr;
        if (screen != nullptr)
            refreshRate = qFloor(screen->refreshRate() * 1000);
    }
    if (refreshRate <= 0)
        refreshRate = kFallbackRefreshRateMilliHz;

    const QWaylandOutputMode mode(size, refreshRate);
    if (!mode.isValid())
        return;

    // Both calls are required and in this order: `setCurrentMode` looks the
    // mode up in the list and warns "Cannot set an unknown QWaylandOutput mode
    // as current" if it is not already there, leaving the output unchanged.
    addMode(mode, /*preferred=*/true);
    setCurrentMode(mode);
}
