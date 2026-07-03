// Shared vim-style list navigation for the git surface's master lists.
//
// BranchListView and CommitListView both walk a ListView with the same
// motion vocabulary — j/k, gg/G, Ctrl+D/Ctrl+U half-page — plus the same
// gg-pending state machine (first g arms, second g jumps, any other key or
// a 400ms timeout cancels). This component is that one shared behavior;
// the views instantiate it and delegate to `handleKey` FIRST in their
// Keys.onPressed (after Tab interception), keeping only their view-specific
// keys (Enter/l/h, p/P, b, Esc) local.
//
// Non-visual: key events stay with the focused view (QML delivers keys to
// the focus item, not to helpers), so this exposes a plain function rather
// than trying to claim focus itself.

import QtQuick

Item {
    id: root
    visible: false

    // The ListView this nav drives. Injected by the owning view.
    required property var view
    // Delegate row pitch — used by the half-page math. Must match the
    // owning view's delegate height so Ctrl+D/U jumps track real rows.
    property int rowHeight: 34

    readonly property int halfPage: Math.max(
        1, Math.floor(view.height / (2 * root.rowHeight)))

    // `gg` pending flag — set by the first `g`, consumed by the second,
    // cleared after a short window so a lone `g` doesn't arm forever.
    property bool _gPending: false

    Timer {
        id: gReset
        interval: 400
        onTriggered: root._gPending = false
    }

    // Process one key event. Returns true when the key was a nav motion
    // (caller sets event.accepted and returns); false when the caller's own
    // key handling should run. Must be called for EVERY key the view
    // receives (after Tab interception) — the gg-cancel logic depends on
    // seeing the interleaved non-g keys.
    function handleKey(event) {
        // Any key other than a bare `g` cancels a pending `gg` (vim
        // semantics — an interleaved motion must not let a later `g`
        // complete a stale `gg` jump).
        if (event.key !== Qt.Key_G || (event.modifiers & Qt.ShiftModifier)) {
            root._gPending = false;
            gReset.stop();
        }
        switch (event.key) {
        case Qt.Key_J:
            view.incrementCurrentIndex();
            return true;
        case Qt.Key_K:
            view.decrementCurrentIndex();
            return true;
        case Qt.Key_G:
            if (event.modifiers & Qt.ShiftModifier) {
                // G — jump to the last loaded row.
                view.currentIndex = view.count - 1;
            } else if (root._gPending) {
                // gg — jump to the first.
                root._gPending = false;
                gReset.stop();
                view.currentIndex = 0;
            } else {
                root._gPending = true;
                gReset.restart();
            }
            return true;
        case Qt.Key_D:
            if (event.modifiers & Qt.ControlModifier) {
                view.currentIndex = Math.min(view.count - 1,
                                             view.currentIndex + root.halfPage);
                return true;
            }
            return false;
        case Qt.Key_U:
            if (event.modifiers & Qt.ControlModifier) {
                view.currentIndex = Math.max(0, view.currentIndex - root.halfPage);
                return true;
            }
            return false;
        }
        return false;
    }
}
