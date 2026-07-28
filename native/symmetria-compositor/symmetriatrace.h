// SPDX-License-Identifier: GPL-3.0-or-later
//
// One-line stderr tracing for this plugin, shared by every class in it.
//
// WHY NOT qWarning/qCDebug. The HOST application installs a Qt message handler
// that routes these away — the IDE swallows `qWarning` and QML `console.log`
// alike. A plugin whose failure mode is "silently does nothing" (a clipboard
// that does not bridge, a scroll event that goes nowhere) needs a channel that
// does not depend on the host's logging choices, so it writes straight to
// stderr. The IDE's dev launcher redirects that to a file, which is where
// these lines are read from.
//
// Off unless SYMMETRIA_COMPOSITOR_DEBUG is set, checked once.

#pragma once

#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace symmetria {

// Truthiness, not mere presence. Everything documents this as `=1`, so someone
// disabling it with `=0` must actually get silence — otherwise they get
// per-wheel-event spam on a pointer hot path and no way to tell why.
inline bool traceEnabled()
{
    static const bool enabled = [] {
        const char *value = std::getenv("SYMMETRIA_COMPOSITOR_DEBUG");
        return value != nullptr && value[0] != '\0' && std::strcmp(value, "0") != 0;
    }();
    return enabled;
}

// The format attribute is not decoration: these are printf-style varargs, so a
// format/argument mismatch is silent undefined behaviour. Call sites already
// migrated once from `trace(what, count)` to format strings — exactly the edit
// this makes the compiler police.
#if defined(__GNUC__) || defined(__clang__)
[[gnu::format(printf, 1, 2)]]
#endif
inline void trace(const char *format, ...)
{
    if (!traceEnabled())
        return;
    std::fprintf(stderr, "[symmetria-compositor] ");
    va_list args;
    va_start(args, format);
    std::vfprintf(stderr, format, args);
    va_end(args);
    std::fprintf(stderr, "\n");
}

} // namespace symmetria
