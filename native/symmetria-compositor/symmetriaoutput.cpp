// SPDX-License-Identifier: GPL-3.0-or-later
// See symmetriaoutput.h for why this class exists.

#include "symmetriaoutput.h"

#include "symmetriatrace.h"

#include <QtCore/QDebug>
#include <QtCore/QtMath>
#include <QtGui/QScreen>
#include <QtGui/QWindow>
#include <QtQuick/QQuickWindow>
#include <QtWaylandCompositor/QWaylandCompositor>
#include <QtWaylandCompositor/QWaylandOutputMode>
#include <QtWaylandCompositor/QWaylandSurface>

namespace {

// Refresh rate is in mHz over the wire. 60Hz is only the last resort for an
// output with no window and no prior mode; it affects nothing we rely on
// (clients read it for frame pacing, and ours renders on the host's vsync
// regardless), but a zero would make the mode INVALID and the whole call a
// silent no-op.
constexpr int kFallbackRefreshRateMilliHz = 60000;

// 20Hz while the host is not drawing. The number is a floor on how long
// anything waiting for a frame can wait — an agent's screenshot lands within
// one interval — traded against how much compositing an animating page can
// waste while nobody is looking at it. 60Hz would spend a full render budget on
// an invisible surface; a few hundred ms would make agent capture feel broken
// even though it works. Static pages cost nothing at any rate, since a client
// only asks for a callback when it has something to draw.
constexpr int kDefaultStalledFrameIntervalMs = 50;

} // namespace

SymmetriaOutput::SymmetriaOutput()
{
    m_frameWatchdog.setInterval(kDefaultStalledFrameIntervalMs);
    m_frameWatchdog.setTimerType(Qt::CoarseTimer);
    connect(&m_frameWatchdog, &QTimer::timeout, this,
            &SymmetriaOutput::pumpFrameCallbacksIfStalled);
    // `window` and `compositor` are QML-assigned properties, so both are null
    // right now and each watch has to be (re)attached whenever it lands.
    connect(this, &QWaylandOutput::windowChanged, this,
            &SymmetriaOutput::rewireRenderWatch);
    connect(this, &QWaylandOutput::compositorChanged, this,
            &SymmetriaOutput::rewireSurfaceWatch);
    // Deliberately NOT started here — see `updateWatchdogEnabled`. It runs only
    // while a client actually has a surface on this output.
}

int SymmetriaOutput::stalledFrameIntervalMs() const
{
    return m_frameWatchdog.interval();
}

void SymmetriaOutput::setStalledFrameIntervalMs(int intervalMs)
{
    // A non-positive interval would turn the watchdog into a busy loop that
    // fires on every event-loop pass, so it is refused rather than clamped —
    // silently substituting a value would hide the mistake behind a machine
    // that merely runs hot.
    if (intervalMs <= 0) {
        qWarning("SymmetriaOutput: stalledFrameIntervalMs must be positive, "
                 "ignoring %d",
                 intervalMs);
        return;
    }
    if (m_frameWatchdog.interval() == intervalMs)
        return;
    m_frameWatchdog.setInterval(intervalMs);
    Q_EMIT stalledFrameIntervalMsChanged();
}

void SymmetriaOutput::initialize()
{
    QWaylandQuickOutput::initialize();
    // Both watches are (re)attached here rather than only from their property
    // signals — see the header for why the signals alone were not enough.
    rewireRenderWatch();
    rewireSurfaceWatch();
}

void SymmetriaOutput::rewireRenderWatch()
{
    disconnect(m_renderWatch);

    auto *quickWindow = qobject_cast<QQuickWindow *>(window());
    if (quickWindow == nullptr)
        return;

    // `afterRendering` is the same signal Qt's own `doFrameCallbacks` hangs
    // off, so observing it means "a real scene-graph frame completed, and Qt
    // has already sent the callbacks for it". Emitted on the RENDER thread; the
    // connection is left implicit (queued, since `this` lives on the GUI
    // thread) so the flag is only ever touched from one thread.
    m_renderWatch = connect(quickWindow, &QQuickWindow::afterRendering, this,
                            [this] { m_renderedSinceLastTick = true; });
}

void SymmetriaOutput::rewireSurfaceWatch()
{
    disconnect(m_surfaceAdded);
    disconnect(m_surfaceRemoved);

    QWaylandCompositor *comp = compositor();
    if (comp == nullptr) {
        m_frameWatchdog.stop();
        return;
    }

    m_surfaceAdded = connect(comp, &QWaylandCompositor::surfaceCreated, this,
                             &SymmetriaOutput::updateWatchdogEnabled);
    // QUEUED on purpose: `surfaceAboutToBeDestroyed` fires while the surface is
    // still in `surfaces()`, so a direct re-count would see the doomed surface
    // and leave the watchdog running forever after the last one closed.
    m_surfaceRemoved =
        connect(comp, &QWaylandCompositor::surfaceAboutToBeDestroyed, this,
                &SymmetriaOutput::updateWatchdogEnabled, Qt::QueuedConnection);

    updateWatchdogEnabled();
}

void SymmetriaOutput::updateWatchdogEnabled()
{
    const QWaylandCompositor *comp = compositor();
    const bool wanted = comp != nullptr && !comp->surfaces().isEmpty();

    if (symmetria::traceEnabled())
        symmetria::trace("frame watchdog: re-evaluated (compositor=%p surfaces=%d "
                         "wanted=%d active=%d)",
                         static_cast<const void *>(comp),
                         comp != nullptr ? int(comp->surfaces().size()) : -1,
                         int(wanted), int(m_frameWatchdog.isActive()));

    if (wanted == m_frameWatchdog.isActive())
        return;

    if (symmetria::traceEnabled())
        symmetria::trace("frame watchdog: %s", wanted ? "armed" : "disarmed");

    if (wanted) {
        // Assume stalled until a real frame proves otherwise, so a browser
        // opened while the IDE already sits on an inactive workspace — the
        // exact case this exists for — is unblocked on the very first tick
        // rather than after an extra interval.
        m_renderedSinceLastTick = false;
        m_frameWatchdog.start();
    } else {
        m_frameWatchdog.stop();
    }
}

void SymmetriaOutput::pumpFrameCallbacksIfStalled()
{
    // The host is drawing; Qt's automatic path already sent this frame's
    // callbacks and sending a second set would only invite the client to draw
    // frames the host has no intention of showing.
    if (m_renderedSinceLastTick) {
        m_renderedSinceLastTick = false;
        return;
    }

    // Mirrors Qt's own guard in `updateStarted()`. An output can outlive or
    // precede its compositor during teardown and QML construction, and both
    // calls below reach through it.
    if (compositor() == nullptr)
        return;

    if (symmetria::traceEnabled())
        symmetria::trace("frame watchdog: host stalled, pumping callbacks");

    // The exact pair the render loop calls, in the same order: `frameStarted`
    // marks each surface as beginning a frame, `sendFrameCallbacks` then
    // releases the clients waiting on one and flushes.
    frameStarted();
    sendFrameCallbacks();
}

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
