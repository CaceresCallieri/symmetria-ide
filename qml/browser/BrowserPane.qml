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

Item {
    id: pane

    // The IDE's Window. The nested output is bound to it (rather than to a
    // Window of its own) precisely so the browser is not a separate window.
    required property var hostWindow
    // Socket name the compositor listens on; Chrome is pointed at it by
    // `chrome_host.chrome_env()`. Comes from the `browserWaylandSocket`
    // context property, which derives it from the IDE's pid.
    required property string socketName

    readonly property int windowCount: surfaces.count
    property int currentIndex: 0

    // Cycling is by TOPLEVEL, not by the slot registry the agents see. The two
    // count different things on purpose: a registry slot is a CDP page target,
    // and `new_page` opens a TAB inside an existing toplevel (measured: 3
    // targets over 2 toplevels), so there is no honest 1:1 mapping to offer.
    function cycleWindow(step) {
        if (surfaces.count === 0)
            return
        currentIndex = (currentIndex + step + surfaces.count) % surfaces.count
        _activateCurrent()
    }

    function _activateCurrent() {
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
                browserCompositor.defaultSeat.keyboardFocus = item.shellSurface.surface
                item.shellSurface.toplevel.sendActivated()
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

    onWidthChanged: _configureAll()
    onHeightChanged: _configureAll()
    onVisibleChanged: if (visible) _activateCurrent()

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
            sizeFollowsWindow: true
            window: pane.hostWindow

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
                if (!item)
                    return
                surfaces.append({ "item": item })
                pane.currentIndex = surfaces.count - 1
                toplevel.sendMaximized(Qt.size(pane.width, pane.height))
                pane._activateCurrent()
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
            anchors.fill: surfaceHost
            // Chrome's menus, omnibox dropdown and tooltips are xdg_popups;
            // without this they never appear at all.
            autoCreatePopupItems: true

            onSurfaceDestroyed: {
                for (var i = 0; i < surfaces.count; i++) {
                    if (surfaces.get(i).item === this) {
                        surfaces.remove(i)
                        break
                    }
                }
                if (pane.currentIndex >= surfaces.count)
                    pane.currentIndex = Math.max(0, surfaces.count - 1)
                destroy()
                pane._activateCurrent()
            }
        }
    }

    // Shown until the first window arrives, so an empty browser surface reads
    // as "nothing open yet" rather than as a rendering failure.
    Text {
        anchors.centerIn: parent
        visible: surfaces.count === 0
        color: "#8888aa"
        text: "No browser windows.\nAn agent opens one with browser_open."
        horizontalAlignment: Text.AlignHCenter
    }
}
