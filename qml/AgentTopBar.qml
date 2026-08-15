// Top chrome bar — the window's two GLOBAL controls, mirroring
// StatusBar.qml at the bottom so the two bracket the content area with
// matched strips.
//
//   - the surface switcher (terminal / editor / agents / git), left edge;
//   - the Local ↔ VPS location toggle, right edge.
//
// It used to carry a third thing: a centered strip of one pill per live
// agent. That moved WHOLE to `AgentThreadRail.qml` on the window's left
// edge — every indicator it carried (sparkle, number, title, worktree,
// browser globe and its attention badge, coordination dot, STT state)
// lives there now. A bar runs out of width after a handful of pills, and
// the rail lists threads instead: as many as the project has, eventually
// including the ones whose CLI is no longer running. Do not reinstate a
// chip strip here; the rail is the index.
//
// Visibility: always. The height stays constant
// (`Theme.size.statusBarHeight`) so the central viewport never jumps.
//
// The two controls are ANCHORED SIBLINGS, not layout members — they pin to
// opposite edges and nothing sits between them.
//
// All colour and typography values bind against the `Theme` singleton
// (`qml/design/Theme.qml`).

import QtQuick

import "design"

Rectangle {
    id: root
    color: Theme.color.bg.bar

    // NO hairline along the bottom edge, and none along StatusBar's top
    // either. There used to be a matched 1px pair bracketing the content,
    // from when every surface was ONE colour and a line was the only thing
    // that could mark the boundary. The surface ladder does that work now —
    // `bg.bar` here against `bg.canvas` below is a real step — so the line
    // became a second, redundant answer to a question already answered.
    //
    // Removing it is what the rounded canvas corner needs, not just tidying:
    // the hairline ran the FULL width, so it cut straight across the arc at
    // the exact row where the corner starts to curve, and struck the side
    // panel as a hard tick where the bar should simply become the panel. Put
    // one back and both corners read as damaged again.

    // Location toggle — Local ↔ VPS context for THIS project. Renders only
    // when the pairing probe found the repo on a registered remote server
    // (controller.vpsAvailable), or defensively while location is already
    // "vps" so the way back never disappears. Pinned to the RIGHT edge
    // (user decision 2026-07-12 — it was briefly leftmost, which crowded
    // the surface switcher and muddled the two controls' reading order;
    // right-aligned it mirrors StatusBar's trailing ⇅ server badge, so
    // both location cues live on the right edge). Shares the flat
    // SegmentedControl with the surface switcher; Ctrl+Shift+U is the chord
    // twin. The overlap caveat this comment used to carry is gone with the
    // chip strip: the bar now holds only these two controls, at opposite
    // edges, and nothing between them can grow into either.
    SegmentedControl {
        id: locationToggle
        anchors.right: root.right
        anchors.rightMargin: Theme.spacing.md
        anchors.verticalCenter: root.verticalCenter
        z: 1
        visible: controller.vpsAvailable || controller.location === "vps"

        // Icon + active-only label, matching the surface switcher (user
        // decision 2026-08-13, reversing this control's first cut). The
        // earlier argument for keeping both words was that the saving is
        // small (two short words) while the cost of misreading which
        // location your commands run in is not -- a wrong surface is
        // obvious immediately, a wrong location only after you run
        // something. The counter-argument that won: the ACTIVE half is
        // still spelled out in words, so the state you are in is never the
        // one you have to infer from a glyph; only the state you are not in
        // is, and that one is named the instant you move to it.
        //
        // It stays SEGMENTED rather than collapsing to a cycling label
        // because the axis is expected to grow past two: with several
        // registered machines the right control is a dropdown, and a
        // segmented row degrades into one far more naturally than a
        // click-to-cycle label, which stops working entirely at N > 2.
        segments: [
            { key: "local", label: "Local", icon: Theme.glyph.location.local },
            { key: "vps", label: "VPS", icon: Theme.glyph.location.vps },
        ]
        current: controller.location
        onActivated: key => controller.set_location(key)
    }

    // Surface switcher — terminal / editor / agents, pinned at the bar's
    // left edge (moved here from StatusBar's center per the 2026-06-11
    // layout decision; the location toggle lives on the opposite edge so
    // the two controls never crowd each other). Anchored rather than laid
    // out, for the same reason as the toggle: two controls at opposite
    // edges need no layout between them. Clicking is a convenience; the
    // chords (Ctrl+Shift+E toggle, Ctrl+1..5 agent focus) remain primary.
    SegmentedControl {
        id: surfaceSwitcher
        anchors.left: root.left
        anchors.leftMargin: Theme.spacing.md
        anchors.verticalCenter: root.verticalCenter
        z: 1

        // `key` is the controller's centralSurface value (the wire name stays
        // singular "agent"); `label` and `icon` are display-only.
        //
        // ICON + ACTIVE-ONLY LABEL (2026-08-13). This was four permanently
        // drawn words, the largest single block of chrome text in the IDE.
        // Now each surface keeps a mark and only the current one is named,
        // which is where every editor with a comparable control landed
        // independently (Zed, VS Code, JetBrains all show icons, none show a
        // row of words) - so the switcher reads as navigation rather than as
        // a settings row. A dropdown was considered and rejected here: this
        // is the most-used control in the window, and a dropdown spends a
        // click and a popup on every single use of it.
        segments: [
            { key: "terminal", label: "Terminal", icon: Theme.glyph.surface.terminal },
            { key: "editor", label: "Editor", icon: Theme.glyph.surface.editor },
            { key: "agent", label: "Agents", icon: Theme.glyph.surface.agent },
            // "git" not "history": the surface is dual-mode (a Tab-toggled
            // "changes" working-tree view + the commit log), and it opens on
            // changes — labelling it "history" would mis-name where the chip
            // lands.
            { key: "git", label: "Git", icon: Theme.glyph.surface.git },
            // NB: no "browser" segment, though there COULD be one now — the
            // browser became a real central surface again when Chrome moved
            // into the IDE's nested compositor. It is left out because the
            // browser is agent-owned: you reach it through the agent that
            // opened it (the globe on that agent's row in the thread rail)
            // or with Ctrl+Shift+B. Adding a segment here is a product call, not an
            // impossibility — which is what this note used to claim.
        ]
        current: controller.centralSurface
        onActivated: key => controller.set_central_surface(key)
    }

}
