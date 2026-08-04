// Presentation helpers for subscription usage — shared by the compact
// `UsageIndicator`, the `UsageDetailPopup`, and StatusBar's context segment.
//
// Why a singleton in `design/` rather than functions on each consumer: the
// threshold colour and the reset countdown were BOTH already duplicated once
// (StatusBar carried private `_usageColor` / `_resetCountdown` copies of
// status-line.sh's `get_usage_color` / `format_reset_countdown`), and the panel
// adds a third and fourth caller. Three copies of a threshold table is exactly
// how one of them silently drifts.
//
// It sits beside `Theme` because it is pure presentation with no state and no
// visual of its own, and because `import "design"` already resolves here for
// every chrome component — the alternative (a qmldir in `qml/` root) would
// change how every existing sibling component resolves, for no benefit.
//
// Deliberately NOT merged into Theme: Theme is a token table (values), this is
// behaviour (functions). Keeping the split means a token retune never has to
// reason about formatting logic.

pragma Singleton

import QtQuick

QtObject {
    id: format

    // The provider's real brand mark, as a resolved URL.
    //
    // `Qt.resolvedUrl` against THIS file (qml/design/) rather than a bare
    // relative string: a relative source would resolve against whichever file
    // renders it, which silently breaks the moment a consumer lives anywhere
    // but qml/. Same asset the spawn menu uses for Claude — one copy, one look.
    function providerIcon(provider) {
        return Qt.resolvedUrl(provider === "codex" ? "../assets/openai-icon.svg" : "../assets/claude-icon.svg");
    }

    // Threshold colour for a 0-100 percentage. Mirrors status-line.sh's
    // get_usage_color tiers so the IDE and the in-terminal status line never
    // disagree about what "amber" means.
    function usageColor(pct) {
        if (pct >= 80)
            return Theme.color.usage.crit;
        if (pct >= 50)
            return Theme.color.usage.warn;
        return Theme.color.usage.good;
    }

    // Compact countdown to a unix-epoch-SECONDS reset, given the caller's live
    // `nowMs` clock. "" when there is no known reset, "now" once elapsed.
    //
    // `nowMs` is passed in rather than read from Date.now() here so the value
    // is a real QML binding dependency: a function reading the clock itself
    // would compute once and never update (gotcha #3). Callers tick a Timer
    // into a `nowMs` property and every countdown re-evaluates with it.
    function countdown(resetEpochSec, nowMs) {
        if (!resetEpochSec || resetEpochSec <= 0)
            return "";
        var diff = Math.floor(resetEpochSec - nowMs / 1000);
        if (diff <= 0)
            return "now";
        var d = Math.floor(diff / 86400);
        var h = Math.floor((diff % 86400) / 3600);
        var m = Math.floor((diff % 3600) / 60);
        if (d > 0)
            return d + "d" + h + "h";
        if (h > 0)
            return h + "h" + m + "m";
        return m + "m";
    }

    // How long ago an observation was made ("just now", "4m ago", "2h ago").
    // The panel's honesty valve: a poll can fail silently for a while, and the
    // age is what tells the user the numbers have stopped moving.
    function age(observedAtSec, nowMs) {
        if (!observedAtSec || observedAtSec <= 0)
            return "never";
        var diff = Math.floor(nowMs / 1000 - observedAtSec);
        if (diff < 60)
            return "just now";
        var m = Math.floor(diff / 60);
        if (m < 60)
            return m + "m ago";
        var h = Math.floor(m / 60);
        if (h < 24)
            return h + "h ago";
        return Math.floor(h / 24) + "d ago";
    }

    // Human wording for a provider fetch failure. The raw reasons are stable
    // machine strings from usage_providers; this is the only place they become
    // user-facing text.
    function errorText(reason) {
        switch (reason) {
        case "":
            return "";
        case "offline":
            return "no connection";
        case "auth":
            return "token refreshing";
        case "no-credentials":
            return "not signed in";
        case "not-installed":
            return "CLI not installed";
        case "timeout":
            return "timed out";
        case "bad-response":
            return "unexpected response";
        default:
            return reason;
        }
    }
}
