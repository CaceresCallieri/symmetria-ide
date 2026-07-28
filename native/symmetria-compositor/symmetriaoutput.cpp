// SPDX-License-Identifier: GPL-3.0-or-later
// See symmetriaoutput.h for why this class exists.

#include "symmetriaoutput.h"

#include <QtCore/QDebug>
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

bool SymmetriaOutput::hasMode() const
{
    return currentMode().isValid();
}

QSize SymmetriaOutput::modeSize() const
{
    return currentMode().size();
}

void SymmetriaOutput::setModeSize(const QSize &size)
{
    // A pane still being laid out is 0-sized, and `QWaylandOutputMode` rejects
    // that as invalid — so this would otherwise be a warning per startup frame.
    if (size.isEmpty())
        return;

    // The header's central requirement, and nothing but this notices when it
    // is broken: with `sizeFollowsWindow` on, Qt overwrites whatever we set
    // here on the next window resize. That flag is one line in a different
    // file, and the only symptom of flipping it is a subtly misplaced Chrome
    // popup — no error, no crash. Warned once rather than per push.
    if (sizeFollowsWindow() && !m_warnedAboutSizeFollowsWindow) {
        m_warnedAboutSizeFollowsWindow = true;
        qWarning("SymmetriaOutput: sizeFollowsWindow must be false, or Qt "
                 "overwrites this mode on the next window resize");
    }

    const QWaylandOutputMode existing = currentMode();
    // The no-op that keeps the mode list from growing (see the header): callers
    // are expected to fire this on every geometry change, and most of those
    // resolve to the same size.
    if (existing.isValid() && existing.size() == size)
        return;

    // Screen FIRST, previous mode only as a fallback. The other order freezes
    // the rate at whatever the first push saw — including a value queried
    // before the window had a screen — and moving the IDE to a different-Hz
    // monitor could then never correct it.
    int refreshRate = 0;
    const QWindow *hostWindow = window();
    const QScreen *screen = hostWindow != nullptr ? hostWindow->screen() : nullptr;
    if (screen != nullptr)
        refreshRate = qFloor(screen->refreshRate() * 1000);
    if (refreshRate <= 0 && existing.isValid())
        refreshRate = existing.refreshRate();
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
