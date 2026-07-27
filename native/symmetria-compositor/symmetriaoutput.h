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

#pragma once

#include <QtCore/QSize>
#include <QtQml/qqmlregistration.h>
#include <QtWaylandCompositor/QWaylandQuickOutput>

class SymmetriaOutput : public QWaylandQuickOutput
{
    Q_OBJECT
    QML_NAMED_ELEMENT(SymmetriaOutput)

public:
    // Sets the output's resolution, in PHYSICAL pixels — the unit `wl_output`'s
    // mode event takes. Clients divide it by the advertised integer
    // `scaleFactor` to get the logical size they lay out against, so pass
    // `paneSizeInLogicalPixels * scaleFactor` to make a client's logical screen
    // match the pane one-to-one.
    //
    // Ignores an empty size and a size already current, so it is safe to call
    // on every geometry change. Requires `sizeFollowsWindow: false`, or Qt
    // overwrites the mode on the next window resize.
    Q_INVOKABLE void setModeSize(const QSize &size);
};
