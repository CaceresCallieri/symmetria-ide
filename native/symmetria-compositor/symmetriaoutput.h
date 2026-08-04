// SPDX-License-Identifier: GPL-3.0-or-later
//
// `SymmetriaOutput` — a WaylandOutput whose resolution can be set from QML.
//
// WHY THIS EXISTS. The nested output is the "screen" Chrome believes it is on,
// and Chrome makes its OWN layout decisions from it — most visibly, whether an
// omnibox dropdown has room below the omnibox or has to flip up over it. Qt's
// compositor does not second-guess any of that: `XdgPopupIntegration` places
// every popup at `unconstrainedPosition` and carries a literal
// `//TODO check positioner constraints etc... sliding, flipping`. So the
// output description is not one input among several — it is the ONLY lever we
// have over popup placement.
//
// Stock `WaylandOutput` can only describe the whole host WINDOW, via
// `sizeFollowsWindow`. The browser occupies a PANE inside that window (the
// side panel and the top/status bars are ours), so the window is the wrong
// rectangle, and the mismatch is not subtle: measured live at a 1592x916
// window with a 1311x868 pane, Chrome reported a 1273x733 screen hosting its
// own 1311x868 window. A window 135px TALLER than its screen leaves Chrome's
// dropdown logic no room it can trust, which is exactly the reported symptom.
//
// WHY IT HAS TO BE C++. `WaylandOutput.geometry` is READ-only (no WRITE in the
// Q_PROPERTY) and `setCurrentMode` is not Q_INVOKABLE, so QML cannot express a
// size at all. `availableGeometry` IS writable and looks like the answer, but
// it is a dead end: there is no available-geometry request in the `wl_output`
// protocol, so it never reaches the client — Qt uses it only internally, for
// placing its own maximized windows.
//
// ⚠ MODES ACCUMULATE. Qt's own resize path mutates the current mode in place
// (`QWaylandOutputMode::setSize` + `modes.replace`), but that setter is private
// with `QWaylandOutputPrivate` as its only friend. The public API offers just
// `addMode`, which APPENDS, and every change re-broadcasts the whole list to
// every client. So each distinct size costs one permanent entry: fine for the
// handful a real session produces (window resize, side panel toggling), which
// is why the caller must debounce rather than configure per frame. Repeats are
// free — `addMode` de-duplicates, and the no-op check below means a settled
// size costs nothing at all.

// ── The second job: keeping the client alive when the host stops drawing ──
//
// Qt drives a nested client's frame callbacks entirely from the host window's
// render loop: `initialize()` wires `QQuickWindow::beforeSynchronizing` to
// `updateStarted()` (which calls `frameStarted()`) and `afterRendering` to
// `doFrameCallbacks()` (which calls `sendFrameCallbacks()` when
// `automaticFrameCallback` is on). Nothing else ever sends one.
//
// Normally that self-sustains: the client commits a buffer, the item updates,
// the window renders, a frame callback goes back, the client commits again. Let
// the window stop being drawn and the cycle breaks and CANNOT restart, because
// the only thing that would send the next callback is the render that is no
// longer happening. It is a deadlock, not slowness, which is why the
// measurement is a clean zero rather than a low number.
//
// ⚠ THE TRIGGER IS "NOT PRODUCING FRAMES", NOT "ON ANOTHER WORKSPACE". An
// inactive Hyprland workspace is the obvious case, but a Qt window only renders
// when something in it is dirty — so an IDE sitting in full view on the current
// workspace, showing an idle terminal, renders a handful of frames a second and
// starves the client just the same. Measured: with the IDE VISIBLE and the
// browser pane hidden behind the terminal surface, the watchdog still found no
// host frame on 66 of ~80 ticks, i.e. the host was drawing at roughly 3.5fps.
// Do not narrow this to a workspace check.
//
// Measured on 2026-07-28, dev IDE on workspace 6 with workspace 8 active: a
// requestAnimationFrame counter returned **0 ticks in 1007ms** while the page
// still reported itself visible, and `Page.captureScreenshot` never returned —
// four attempts, all hitting chrome-devtools-mcp's 180s protocol timeout, on
// both a static and a video page, foreground and background. An agent that
// screenshots the browser while the user is on another workspace simply hangs,
// which is the same failure class that disqualified QtWebEngine.
//
// THE FIX IS A WATCHDOG, not a replacement for the automatic path. A timer on
// the GUI thread checks whether a real scene-graph frame happened since the
// last tick; if one did, it does nothing at all and Qt's own path stays in
// charge. If none did, it calls `frameStarted()` + `sendFrameCallbacks()`
// itself — exactly the pair the render loop would have called, in the same
// order — which unblocks the client and keeps it unblocked.
//
// Verified end to end against the failing condition (IDE on workspace 6,
// workspace 10 active): rAF went 0 -> 61 ticks/s and `Page.captureScreenshot`
// went 180s-timeout -> 0.1s. Note 61, not 20: a client's own frame rate is not
// the callback rate. Chromium free-runs on its own BeginFrame source once it is
// no longer BLOCKED, so this interval governs how fast the deadlock breaks, not
// how fast the page then runs.
//
// WHY NOT force the host to keep rendering instead (`window->update()` on a
// timer): the same deadlock exists one level up. Hyprland sends no frame
// callbacks to a window it is not displaying, so Qt's threaded render loop
// blocks too, and asking it to render changes nothing.
//
// ⚠ Frame callbacks PERMIT drawing, they do not force it — a client asks for
// one only when it has something to draw. So this costs nothing on a static
// page no matter the rate, and costs real work only on an animating one, which
// is also the case where an agent plausibly wants those frames. That asymmetry
// is what makes a fixed interval defensible instead of adaptive.

#pragma once

#include <QtCore/QSize>
#include <QtCore/QTimer>
#include <QtQml/qqmlregistration.h>
#include <QtWaylandCompositor/QWaylandQuickOutput>

class SymmetriaOutput : public QWaylandQuickOutput
{
    Q_OBJECT
    QML_NAMED_ELEMENT(SymmetriaOutput)

    // How often to hand out a frame callback while the host window is NOT
    // rendering. Also the worst-case latency of anything that waits on a frame
    // — `Page.captureScreenshot` most importantly — so it is a responsiveness
    // floor, not just a throttle.
    //
    // Exposed so the trade-off can be retuned from QML without a rebuild of a
    // packaged plugin: lower means snappier agent capture and more wasted
    // compositing for an animating page nobody is looking at; higher, the
    // reverse. See the default's rationale in the .cpp.
    Q_PROPERTY(int stalledFrameIntervalMs READ stalledFrameIntervalMs WRITE
                   setStalledFrameIntervalMs NOTIFY stalledFrameIntervalMsChanged)

public:
    // No parent parameter, because the base has none to forward to
    // (`QWaylandQuickOutput()` is parameterless) and QML reparents the object
    // itself after construction.
    SymmetriaOutput();

    int stalledFrameIntervalMs() const;
    void setStalledFrameIntervalMs(int intervalMs);

Q_SIGNALS:
    void stalledFrameIntervalMsChanged();

public:
    // Sets the output's resolution, in PHYSICAL pixels — the unit `wl_output`'s
    // mode event takes. Clients divide it by the advertised integer
    // `scaleFactor` to get the logical size they lay out against, so pass
    // `paneSizeInLogicalPixels * scaleFactor` to make a client's logical screen
    // match the pane one-to-one.
    //
    // ⚠ QML's `size` value type is a QSizeF, and the conversion to QSize at the
    // metacall boundary TRUNCATES — so callers must round rather than hand over
    // a fractional product. That rounding is load-bearing, not cosmetic.
    //
    // Ignores an empty size and a size already current, so it is safe to call
    // on every geometry change. Requires `sizeFollowsWindow: false`, or Qt
    // overwrites the mode on the next window resize — warned about once if it
    // is left on.
    Q_INVOKABLE void setModeSize(const QSize &size);

    // Whether a mode has been set at all. C++ is the single source of truth
    // here: a QML-side "we pushed one" flag would claim success for the early
    // returns above, and with `sizeFollowsWindow` off a mode-less output means
    // a client seeing a screen with no resolution.
    Q_INVOKABLE bool hasMode() const;

    // Current resolution in physical pixels, so a caller can tell a GROWING
    // pane from a shrinking one — the former must not be debounced. Invalid
    // when no mode is set.
    Q_INVOKABLE QSize modeSize() const;

protected:
    // Qt calls this once the output has both a compositor and a window — which
    // is the only moment either is guaranteed. The `compositorChanged` /
    // `windowChanged` signals are ALSO connected, but they proved not to be
    // enough on their own: with the output declared inside the compositor in
    // QML, `compositorChanged` never reached us, so a watch wired only from
    // that signal stayed unarmed and the whole fix silently did nothing.
    void initialize() override;

private:
    // Re-attaches the watches after `window` / `compositor` change. QML assigns
    // both properties after construction, so there is nothing to watch at build
    // time.
    void rewireRenderWatch();
    void rewireSurfaceWatch();
    // Runs the watchdog only while a client actually has a surface here. The
    // browser pane's compositor is built at startup and never torn down (its
    // Loader must stay active or Chrome loses its display), so without this the
    // timer would tick in every IDE for the whole session — and the user runs
    // roughly ten at once, which turns a rounding error into ~200 pointless
    // wakeups a second across the desktop. A project that never browses should
    // pay nothing, which is the same principle that makes Chrome lazy-spawned.
    void updateWatchdogEnabled();
    void pumpFrameCallbacksIfStalled();

    bool m_warnedAboutSizeFollowsWindow = false;

    QTimer m_frameWatchdog;
    // Written by the `afterRendering` handler and read by the watchdog tick.
    // Both run on the GUI thread — `afterRendering` is emitted on the RENDER
    // thread, so the connection is deliberately left queued (Qt's own
    // `doFrameCallbacks` connection is queued for the same reason). A direct
    // connection here would be a data race on this flag.
    bool m_renderedSinceLastTick = false;
    QMetaObject::Connection m_renderWatch;
    QMetaObject::Connection m_surfaceAdded;
    QMetaObject::Connection m_surfaceRemoved;
};
