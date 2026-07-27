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
// recreate. Hiding is free instead — measured with the IDE on an INACTIVE
// workspace and the surface hidden four different ways, Chrome held a full 60Hz
// of requestAnimationFrame and screenshots stayed at ~60ms.
//
// It IS mounted through a permanently-active Loader, for a different reason:
// QML has no conditional imports, so a missing `Symmetria.Compositor` package
// would fail this whole file — and as a direct child of Main.qml that would
// take the entire IDE down over an optional clipboard feature. The Loader
// contains the failure without ever deactivating. Same class of dependency as
// the qmltermwidget fork, which likewise breaks its panes when unpackaged.

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
                    Qt.size(pane.width, pane.height),
                    [XdgToplevel.ActivatedState, XdgToplevel.MaximizedState])
                item.forceActiveFocus()
            }
        }
    }

    function _configureAll() {
        for (var i = 0; i < surfaces.count; i++) {
            var item = surfaces.get(i).item
            if (item && item.shellSurface)
                // sendMaximized, NOT sendFullscreen: fullscreen makes Chrome
                // hide its tabs and omnibox, and the full browser chrome is
                // explicitly wanted — this browser doubles as a surface for
                // showing things to other people.
                item.shellSurface.toplevel.sendMaximized(Qt.size(pane.width, pane.height))
        }
    }

    // Qt.callLater coalesces to one configure per frame. Sending one per pixel
    // of an interactive resize makes Chrome re-lay-out that many times, and
    // with wl_shm (no dmabuf here) every one of those is a CPU buffer copy.
    onWidthChanged: Qt.callLater(_configureAll)
    onHeightChanged: Qt.callLater(_configureAll)
    onVisibleChanged: if (visible) activateCurrent()

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

        WaylandOutput {
            compositor: browserCompositor
            window: pane.hostWindow

            // ⚠ KNOWN LIMITATION: the output tracks the whole IDE WINDOW while
            // toplevels are configured to the PANE rect. The output is the
            // "screen" Chrome believes it is on, so `screen.availWidth/Height`
            // are wrong and xdg-popups are constrained/flipped against the
            // window's edges rather than the pane's — the reported symptom is
            // the omnibox dropdown landing misplaced and clipped.
            //
            // NOT fixed by simply setting `geometry` to the pane rect: that
            // property's unit convention against a non-1 `scaleFactor` is
            // unverified here, and guessing it wrong renders the browser at
            // half or double size. Needs a live check before changing.
            sizeFollowsWindow: true

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
                toplevel.sendMaximized(Qt.size(pane.width, pane.height))
                pane.activateCurrent()
            }
        }
    }

    Item {
        id: surfaceHost
        anchors.fill: parent
    }

    Component {
        id: surfaceComponent

        ShellSurfaceItem {
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
