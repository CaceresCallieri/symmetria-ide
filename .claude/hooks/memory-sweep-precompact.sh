#!/usr/bin/env bash
set -euo pipefail

# Registered in .claude/settings.json under PreCompact with an absolute path.
# Claude Code does not expand env vars or relative paths in hook command
# strings, so if this repo moves to a different home or user, update the
# command path in .claude/settings.json to match the new absolute location.

# Memory sweep checkpoint — fires before context compaction (manual /compact
# or auto-compact at context limit). Last chance to persist learnings before
# the conversation is summarized away.
#
# Strict leanness gates baked into the reminder: the DEFAULT outcome is
# "save nothing." This is intentional — the user wants the memory system to
# stay lean, with only learnings that genuinely benefit future agents.

cat << 'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "PreCompact",
    "additionalContext": "<system-reminder>\n[MEMORY SWEEP CHECKPOINT — pre-compaction]\n\nContext is about to be compacted. Briefly scan the work since session start (or since the last sweep) for items worth persisting to `.claude/memory/` or promoting to `.claude/rules/`.\n\nGATES — only persist a learning if ALL are true:\n1. A future agent would be SURPRISED, STUCK, or WASTE TIME without it.\n2. It cannot be re-derived from the code, git log, or existing CLAUDE.md / .claude/rules/ / .claude/memory/ in <2 minutes.\n3. It is a stable pattern, pitfall, or decision — NOT ephemeral task state, current conversation context, or work-in-progress.\n4. No existing memory or rule already captures it. If similar exists, UPDATE that file instead of creating a new one (no two sources of truth).\n\nDEFAULT OUTCOME: nothing new to save. \"No memory needed before compact\" is the correct answer most of the time. Do NOT save things \"just in case\" — they become noise that drowns out real signal and mislead future agents when they go stale.\n\nIf something qualifies:\n- Follow the promotion lifecycle in `.claude/rules/memory_doctrine.md` (scratch → memory → rule → pointer).\n- Place new files in the correct subfolder (`feedback/`, `project/{meta,active,shipped}/`, `reference/{host,nvim-rpc,qt-pyside,agent-sdk}/`, etc.).\n- Add ONE bullet (≤150 chars) to `MEMORY.md` per new file, under the right section.\n- Keep `MEMORY.md` ≤200 lines — prune stale shipped-system bullets first if needed.\n- If a memory has graduated into shipped code or a rule file, shrink it to a one-line pointer or remove it.\n\nReport one line to the user: either what you saved and why, or \"memory sweep: nothing new worth persisting.\" Then proceed with compaction.\n</system-reminder>"
  }
}
EOF

exit 0
