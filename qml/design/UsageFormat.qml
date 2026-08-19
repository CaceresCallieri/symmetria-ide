// Presentation helpers for subscription usage — shared by the compact
// `UsageIndicator`, the `UsageDetailPopup`, and StatusBar's context segment.
//
// The two ELAPSED-TIME helpers (`age`, `duration`) have outgrown that name and
// are shared with `AgentThreadRail` as well. They stayed here rather than
// moving to a new singleton for the reason the whole file exists: "how long
// ago" had already been written twice before the rail wanted it a third time,
// and a `TimeFormat.qml` holding two functions would just be a second place a
// future caller has to know to look.
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
    // An explicit map, NOT a `provider === "codex" ? … : claude` ternary: this
    // module is built for growth, and a ternary silently renders every future
    // provider as Anthropic — a wrong-brand attribution, which is worse than a
    // missing icon. An unknown provider gets "", and an empty `source` draws
    // nothing.
    readonly property var _providerIcons: ({
        "claude": "../assets/claude-icon.svg",
        "codex": "../assets/openai-icon.svg",
        "opencode": "../assets/opencode-icon.svg"
    })

    function providerIcon(provider) {
        var file = format._providerIcons[provider];
        return file ? Qt.resolvedUrl(file) : "";
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

    // Bare elapsed time since a unix-epoch-SECONDS stamp ("now", "22m", "2h",
    // "3d") — `age` without the "ago", for a caller that supplies the meaning
    // itself. The thread rail is that caller: the same string there reads
    // "working for 22m" on an agent that is running and "idle for 22m" on one
    // that is not, and only the row knows which.
    //
    // SINGLE-UNIT on purpose, where `countdown` composes two ("2h30m"). That
    // one has a whole status-bar segment to fill and a deadline where the
    // minutes matter; this one sits at the right edge of a 220px rail row, and
    // the reader is asking "recently, or a while ago" — a second unit costs
    // width the title needs and answers a question nobody asked.
    //
    // `nowMs` is the caller's live clock for the same reason it is on `age`
    // and `countdown`: read here, `Date.now()` would not be a binding
    // dependency and every duration would freeze at its first evaluation
    // (gotcha #3).
    function duration(sinceEpochSec, nowMs) {
        if (!sinceEpochSec || sinceEpochSec <= 0)
            return "";
        var diff = Math.floor(nowMs / 1000 - sinceEpochSec);
        // Guard the clock going backwards (an NTP step, a suspend/resume)
        // rather than rendering "-3m", which reads as a bug in the rail.
        if (diff < 60)
            return "now";
        var m = Math.floor(diff / 60);
        if (m < 60)
            return m + "m";
        var h = Math.floor(m / 60);
        if (h < 24)
            return h + "h";
        return Math.floor(h / 24) + "d";
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
        case "spawn-failed":
            return "could not start CLI";
        case "timeout":
            return "timed out";
        case "bad-response":
            return "unexpected response";
        case "publish-failed":
            return "could not save result";
        case "unknown-provider":
            return "unsupported provider";
        default:
            // `http-<code>` is generated, not enumerated, so it cannot have a
            // case of its own. Anything else still reaches the user, so it
            // degrades to a generic word rather than a machine string.
            if (reason.indexOf("http-") === 0)
                return "server error " + reason.slice(5);
            return "unavailable";
        }
    }
}
