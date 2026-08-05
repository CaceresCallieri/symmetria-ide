// The agentic browser, rendered INSIDE the IDE.
//
// This is a nested Wayland compositor. Real, unmodified Google Chrome runs as
// an ordinary Wayland client of it (chrome_host.py hands it our socket via
// WAYLAND_DISPLAY), and its windows arrive here as xdg-shell toplevels that we
// draw as items in our own scene graph.
//
// WHY THIS AND NOT AN EMBEDDED ENGINE. QtWebEngine could not do the job:
// `Target.createTarget` — the `new_page` every chrome-devtools-mcp workflow
// starts from — is unsupported, screenshots stall when the IDE is off-workspace,
// and there are no extensions, no Widevine, and no real dashboard logins. Real
// Chrome has all of it. The interim answer was an external window pinned by a
// Hyprland rule; this replaces that, and the containment gets STRONGER rather
// than weaker: `hyprctl clients` does not list the browser at all, so there is
// no window to escape and no map-time race to lose.
//
// ⚠ THIS FILE'S LOADER MUST NEVER BE DEACTIVATED. The compositor owns Chrome's
// Wayland connection; unloading it kills every surface and takes the browser
// process's display with it. Same rule, and the same reason, as the fixed-index
// Repeater behind the agent terminal panes: a live client is not a view you can
// recreate. Hiding is the correct gate instead — and it is free ONLY because
// SymmetriaOutput drives frame callbacks itself (a 20Hz watchdog) when the host
// stops producing frames. ⚠ The measurement this comment used to cite — a full
// 60Hz of requestAnimationFrame while hidden and off-workspace — was re-run
// 2026-07-28 and INVERTED: 0 rAF ticks in 1007ms, screenshots never returning.
// That claim is retracted; without the watchdog a hidden pane stalls the client
// permanently. See symmetriaoutput.h and
// .claude/memory/reference/qt-pyside/nested_compositor_frame_starvation.md.
//
// It IS mounted through a permanently-active Loader, for a different reason:
// QML has no conditional imports, so a missing `Symmetria.Compositor` package
// would fail this whole file — and as a direct child of Main.qml that would
// take the entire IDE down over an optional clipboard feature. The Loader
// contains the failure without ever deactivating. Same class of dependency as
// the qmltermwidget fork, which likewise breaks its panes when unpackaged.

// The surface delegate below is an inline `Component`, and it reaches for ids
// declared out here (`pane`, `surfaces`, `surfaceHost`). Without this pragma
// those resolve through the dynamic scope chain at run time — the `unqualified`
// findings that qmllint reports and that the QML→C++ compiler cannot lower.
// Safe here specifically because nothing in this file is a view delegate: the
// pragma also stops `index`/`model` being injected implicitly, which would
// silently break a Repeater or ListView delegate that relied on them.
pragma ComponentBehavior: Bound

import QtQuick
import QtWayland.Compositor
import QtWayland.Compositor.XdgShell
import Symmetria.Compositor
import "../design"

Item {
    id: pane

    // The IDE's Window. The nested output is bound to it (rather than to a
    // Window of its own) precisely so the browser is not a separate window.
    required property var hostWindow
    // Socket name the compositor listens on; Chrome is pointed at it by
    // `chrome_host.chrome_env()`. Comes from the `browserWaylandSocket`
    // context property, which derives it from the IDE's pid.
    required property string socketName
    // The HOST session's keyboard layout, as an RMLVO map resolved by
    // `keyboard_layout.resolve_host_keymap()`. Injected rather than read off
    // the context property so the compositor block below stays qualified.
    // Load-bearing for the WHOLE IDE, not just the browser — see the
    // Component.onCompleted on the compositor.
    required property var hostKeymap

    property int currentIndex: 0

    // Cycling is by TOPLEVEL, not by the slot registry the agents see. The two
    // count different things on purpose: a registry slot is a CDP page target,
    // and `new_page` opens a TAB inside an existing toplevel (measured: 3
    // targets over 2 toplevels), so there is no honest 1:1 mapping to offer.
    function cycleWindow(step) {
        if (surfaces.count === 0)
            return
        currentIndex = (currentIndex + step + surfaces.count) % surfaces.count
        activateCurrent()
    }

    function activateCurrent() {
        for (var i = 0; i < surfaces.count; i++) {
            var item = surfaces.get(i).item
            if (!item || !item.shellSurface)
                continue
            item.visible = (i === currentIndex)
            if (i === currentIndex && pane.visible) {
                // Two separate requirements, and missing either one looks like
                // a broken browser rather than a focus bug: the seat's keyboard
                // focus is what routes our key events to Chrome, and the
                // toplevel's ACTIVATED state is what makes Chrome consider its
                // own document focused. Without ACTIVATED, Chrome refuses the
                // whole async clipboard API with a bare NotAllowedError.
                //
                // ⚠ There is NO `sendActivated()` — verified against both the
                // Qt 6.11 headers and XdgShell's qmltypes, which expose only
                // sendConfigure/Close/Maximized/Unmaximized/Fullscreen/Resizing
                // (`activated` is a READ-only property). Calling it threw a
                // TypeError that aborted this loop mid-iteration, so every
                // later window kept its old visibility and forceActiveFocus()
                // below never ran at all — invisible in a one-window session.
                // The state list must be passed through sendConfigure instead,
                // and MaximizedState has to be repeated there or the configure
                // silently un-maximizes the window.
                browserCompositor.defaultSeat.keyboardFocus = item.shellSurface.surface
                item.shellSurface.toplevel.sendConfigure(
                    pane._paneSize(),
                    [XdgToplevel.ActivatedState, XdgToplevel.MaximizedState])
                item.forceActiveFocus()
            }
        }
    }

    // The pane rect, in the RAW logical units every toplevel configure uses.
    // One definition because the unit contract has two halves in different
    // places: configures pass this straight through, while the output mode
    // multiplies it by `scaleFactor` (see SymmetriaOutput). With the size
    // spelled out at each site instead, a change to one was invisible from the
    // others — and the two halves disagreeing IS the bug this pane was fixed
    // for, a window measured against a screen in different units.
    function _paneSize() {
        return Qt.size(pane.width, pane.height)
    }

    function _configureAll() {
        for (var i = 0; i < surfaces.count; i++) {
            var item = surfaces.get(i).item
            if (item && item.shellSurface)
                // sendMaximized, NOT sendFullscreen: fullscreen makes Chrome
                // hide its tabs and omnibox, and the full browser chrome is
                // explicitly wanted — this browser doubles as a surface for
                // showing things to other people.
                item.shellSurface.toplevel.sendMaximized(pane._paneSize())
        }
    }

    // How long the output's mode waits for a resize to settle. The toplevels
    // are configured per FRAME instead, because the two have different costs:
    // a configure is cheap and idempotent, while a mode change permanently
    // appends to the output's mode list and re-broadcasts the whole list to
    // every client (Qt's public API has no in-place setter — see
    // symmetriaoutput.h). One entry per settled size is affordable; one per
    // frame of a drag is not.
    readonly property int outputModeSettleMs: 150

    function _scheduleOutputMode() {
        // GROWTH is never debounced, and the reason is the whole point of this
        // pane. The toplevel is configured per frame, so while the pane grows a
        // lagging mode leaves Chrome's window LARGER than the screen it is told
        // it occupies — the exact state that makes it flip the omnibox dropdown
        // up over the omnibox. Debouncing a shrink is harmless (the screen is
        // merely bigger than the window for a moment); debouncing a grow
        // reinstates the bug for the length of the drag.
        //
        // The first push cannot wait either: with `sizeFollowsWindow` off Qt
        // adds no mode at all, so until one lands a connecting client sees a
        // screen with no resolution.
        var want = browserOutput.wantedModeSize()
        var current = browserOutput.modeSize()
        if (!browserOutput.hasMode()
                || want.width > current.width || want.height > current.height) {
            outputModeSettle.stop()
            browserOutput.syncMode()
        } else {
            outputModeSettle.restart()
        }
    }

    Timer {
        id: outputModeSettle
        interval: pane.outputModeSettleMs
        onTriggered: browserOutput.syncMode()
    }

    // Qt.callLater coalesces to one configure per frame. Sending one per pixel
    // of an interactive resize makes Chrome re-lay-out that many times, and
    // with wl_shm (no dmabuf here) every one of those is a CPU buffer copy.
    onWidthChanged: {
        Qt.callLater(_configureAll)
        _scheduleOutputMode()
    }
    onHeightChanged: {
        Qt.callLater(_configureAll)
        _scheduleOutputMode()
    }
    onVisibleChanged: if (visible) activateCurrent()
    Component.onCompleted: _scheduleOutputMode()

    // Bookkeeping only — the ShellSurfaceItems are parented to `surfaceHost`,
    // not to this model. It exists so cycling has a stable order.
    ListModel { id: surfaces }

    // Ours, not Qt's stock `WaylandCompositor` — the difference is the host
    // clipboard bridge (native/symmetria-compositor). A nested compositor gets
    // its own `wl_data_device` and Qt bridges nothing, so without this a URL
    // copied in the browser could not be pasted into a terminal or agent pane:
    // an island, in the one surface whose whole job is feeding the others.
    SymmetriaCompositor {
        id: browserCompositor
        socketName: pane.socketName

        // WORKAROUND: pin the nested seat to the HOST's keyboard layout.
        //
        // This looks like it only concerns Chrome. It does not — it is what
        // keeps the ENTIRE IDE typing on the user's own layout. Constructing a
        // QWaylandCompositor makes the host window translate keys with THIS
        // seat's keymap instead of the one the real compositor sent, and Qt's
        // default is the xkb default, i.e. US. So on a latam (or any
        // non-US) keyboard, merely loading this pane silently switched every
        // terminal, agent and editor pane to US.
        //
        // Measured 2026-08-04: an EMPTY WaylandCompositor is already enough,
        // and the same window types correctly right up to the instant the
        // compositor is constructed — so lazy-loading the pane is not a fix.
        // The full probe, its numbers and the removal criterion are in
        // src/symmetria_ide/keyboard_layout.py.
        //
        // Assigned imperatively rather than bound: `defaultSeat` is a
        // read-only object property, so `defaultSeat.keymap.layout: x` is not
        // a legal binding target.
        Component.onCompleted: {
            // Guarded because the failure would otherwise be inaudible: a
            // TypeError here aborts the handler at its first line, the pane
            // still loads, and the ONLY symptom is a US keyboard plus one
            // warning in a noisy log. That is the exact shape of
            // .claude/rules/qml_property_must_exist_on_type.md.
            if (!pane.hostKeymap) {
                console.warn("hostKeymap missing — nested seat left on Qt's "
                             + "US default, so the WHOLE IDE will type US");
                return;
            }
            const km = browserCompositor.defaultSeat.keymap;
            // Field-by-field from a list so a partial map cannot half-apply.
            const fields = ["rules", "model", "layout", "variant", "options"];
            for (const field of fields)
                km[field] = pane.hostKeymap[field] || "";
        }

        // Ours, not Qt's stock `WaylandOutput`: only a C++ subclass can set the
        // output's resolution, and this output must describe the PANE rather
        // than the host window. See native/symmetria-compositor/symmetriaoutput.h
        // for the whole argument; the short version is that Chrome decides for
        // ITSELF whether a dropdown fits below the omnibox, using the screen we
        // advertise, and Qt then places the popup wherever Chrome asked without
        // constraining it (`XdgPopupIntegration` is an explicit TODO upstream).
        // Describe the window instead of the pane and Chrome sees its own
        // window overflowing its own screen, so it flips the dropdown up over
        // the omnibox — the reported "se tapa".
        SymmetriaOutput {
            id: browserOutput
            compositor: browserCompositor
            window: pane.hostWindow

            // Must stay false, or Qt overwrites our mode with the window's
            // pixel size on the next resize.
            sizeFollowsWindow: false

            // The single number that decides whether the browser looks sharp.
            //
            // The host monitor runs fractional (scale 1.6), so the IDE window
            // has devicePixelRatio 1.6. If this output advertised scale 1,
            // Chrome would draw 1x buffers that then get stretched ×1.6 on the
            // way to the screen — visibly soft, and the first thing anyone
            // notices about the in-window browser.
            //
            // `wl_output` scale is an INTEGER by protocol, and the nested
            // output advertises no `wp_fractional_scale_v1`, so 1.6 cannot be
            // expressed directly. Rounding UP is what makes it sharp: Chrome
            // renders at 2x and the compositor DOWNSCALES 2 → 1.6
            // (oversampling) instead of upscaling 1 → 1.6 (interpolation).
            scaleFactor: Math.max(1, Math.ceil(pane.hostWindow
                                               ? pane.hostWindow.devicePixelRatio
                                               : 1))

            // The mode this pane size calls for, in PHYSICAL pixels.
            //
            // Multiplying by `scaleFactor` is what puts the client's logical
            // units on the same footing as ours: the client divides the mode by
            // the advertised scale, so it lands back on exactly the pane size
            // we pass to `sendMaximized`. Get this wrong and window and screen
            // are measured in different units — which is the bug this replaced,
            // where a 1311x868 window sat on a 1273x733 screen.
            //
            // Rounded, not truncated: QML's `size` is a QSizeF and the
            // conversion to QSize at the C++ boundary truncates.
            function wantedModeSize() {
                var size = pane._paneSize()
                return Qt.size(
                    Math.round(size.width * browserOutput.scaleFactor),
                    Math.round(size.height * browserOutput.scaleFactor))
            }

            function syncMode() {
                if (pane.width <= 0 || pane.height <= 0)
                    return
                browserOutput.setModeSize(browserOutput.wantedModeSize())
            }

            // Direct, not debounced. A scale change is rare, never per-frame,
            // and delaying it leaves the advertised screen wrong for the whole
            // settle — including the case where the first push happened before
            // this binding had evaluated.
            onScaleFactorChanged: browserOutput.syncMode()
        }

        XdgShell {
            onToplevelCreated: function (toplevel, xdgSurface) {
                var item = surfaceComponent.createObject(surfaceHost, {
                    "shellSurface": xdgSurface
                })
                if (!item) {
                    // Otherwise a live toplevel is silently abandoned: mapped
                    // by the client, never drawn, never cycled to — which the
                    // user reads as "the browser didn't open".
                    console.warn("BrowserPane: could not create a surface item"
                                 + " for a Chrome toplevel")
                    toplevel.sendClose()
                    return
                }
                surfaces.append({ "item": item })
                pane.currentIndex = surfaces.count - 1
                toplevel.sendMaximized(pane._paneSize())
                pane.activateCurrent()
            }
        }
    }

    // Clipped so a client surface can never paint over the IDE's own chrome.
    // Chrome positions its popups against the screen we advertise, and Qt
    // honours that position without constraining it (see SymmetriaOutput), so
    // there is nothing else standing between an oversized popup and the file
    // tree next door.
    Item {
        id: surfaceHost
        anchors.fill: parent
        clip: true
    }

    Component {
        id: surfaceComponent

        // Ours, not Qt's stock `ShellSurfaceItem`, for three things it does
        // that the stock item does not — all of them C++-only, which is the
        // whole reason the subclass exists. Losing either of the first two
        // stops the browser scrolling entirely: wheel events survive
        // quantisation (Qt truncates any `angleDelta` under 12 to a zero-valued
        // axis, which on a high-resolution wheel or a touchpad — neither of
        // which emits the 120-unit steps that arithmetic assumes — is every
        // event), and a `wl_pointer.frame` follows each axis (Chromium only
        // buffers on the axis itself and flushes on the frame). Dragging a
        // scrollbar worked throughout, because press and move touch neither.
        // The third is hover: nothing in Qt calls `setAcceptHoverEvents`, and
        // `hoverEnterEvent`/`hoverMoveEvent` are the only callers of
        // `sendMouseMoveEvent`, so without it the client is told where the
        // pointer is only when a button goes down.
        //
        // ⚠ Hover was once attempted HERE, by walking the item tree assigning
        // `item.hoverEnabled = true`. That property does not exist on any of
        // these types, so the assignment threw on its first line and the walk
        // never ran — do not reintroduce it. See symmetriashellsurfaceitem.h.
        SymmetriaShellSurfaceItem {
            id: surfaceItem
            anchors.fill: surfaceHost
            // Chrome's menus, omnibox dropdown and tooltips are xdg_popups;
            // without this they never appear at all.
            autoCreatePopupItems: true

            onSurfaceDestroyed: {
                // Compared against the delegate's own id rather than `this`:
                // `this` resolves to the scope object in a handler, which is
                // fragile the moment any of this moves into a helper.
                var removed = -1
                for (var i = 0; i < surfaces.count; i++) {
                    if (surfaces.get(i).item === surfaceItem) {
                        removed = i
                        surfaces.remove(i)
                        break
                    }
                }
                // Closing a window BELOW the current one shifts every later
                // entry down, so holding the index still would silently move
                // the user to a different window than the one they were on.
                if (removed >= 0 && removed < pane.currentIndex)
                    pane.currentIndex--
                else if (pane.currentIndex >= surfaces.count)
                    pane.currentIndex = Math.max(0, surfaces.count - 1)
                surfaceItem.destroy()
                pane.activateCurrent()
            }
        }
    }

    // Shown until the first window arrives, so an empty browser surface reads
    // as "nothing open yet" rather than as a rendering failure. This is the
    // NORMAL state of a project that has not browsed — Chrome is lazy-spawned,
    // and eager-spawning to fill this pane would charge every project for a
    // browser it may never use.
    Text {
        anchors.centerIn: parent
        visible: surfaces.count === 0
        color: Theme.color.text.dim
        font.family: editorFontFamily
        renderType: Text.NativeRendering
        text: "No browser windows.\nAn agent opens one with browser_open."
        horizontalAlignment: Text.AlignHCenter
    }
}
