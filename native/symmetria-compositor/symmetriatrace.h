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

namespace symmetria {

inline bool traceEnabled()
{
    static const bool enabled = std::getenv("SYMMETRIA_COMPOSITOR_DEBUG") != nullptr;
    return enabled;
}

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
