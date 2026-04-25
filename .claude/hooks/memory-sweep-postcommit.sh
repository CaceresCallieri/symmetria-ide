#!/usr/bin/env bash

# Memory sweep checkpoint — fires AFTER /commit is invoked. The /commit skill
# marks a logical end-of-work boundary: the right moment to ask "did this
# session teach us anything future agents should know?"
#
# This hook is project-local (symmetria-ide only) — the global /commit skill
# stays project-agnostic. Other projects calling /commit get no memory step
# unless their .claude/settings.json registers an equivalent hook.
#
# Filtering: PostToolUse fires on every Skill invocation. We parse stdin
# JSON and only inject when tool_input.skill == "commit". Silent exit otherwise.

# Read stdin once
INPUT="$(cat)"

# Extract skill name
SKILL_NAME="$(printf '%s' "$INPUT" | jq -r '.tool_input.skill // empty' 2>/dev/null)"

# Only fire for the commit skill — silent no-op for everything else
if [ "$SKILL_NAME" != "commit" ]; then
    exit 0
fi

cat << 'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "<system-reminder>\n[MEMORY SWEEP CHECKPOINT — post-commit]\n\nAfter completing the commit, perform a brief memory sweep on the work just committed. The /commit invocation marks a logical end-of-work — the right moment to ask \"did this session teach us anything future agents should know?\"\n\nGATES — only persist a learning if ALL are true:\n1. A future agent would be SURPRISED, STUCK, or WASTE TIME without it.\n2. It cannot be re-derived from the just-committed code, git log, or existing CLAUDE.md / .claude/rules/ / .claude/memory/ in <2 minutes.\n3. It is a stable pattern, pitfall, or decision — NOT ephemeral task state or work-in-progress.\n4. No existing memory or rule already captures it. If similar exists, UPDATE that file instead of creating a new one (no two sources of truth).\n\nDEFAULT OUTCOME: nothing new to save. \"No memory needed this commit\" is the correct answer most of the time. The committed code is its own documentation — only persist what the code CANNOT say on its own (Qt/PySide quirks, NeoVim RPC landmines, agent-SDK protocol quirks, decisions with non-obvious rationale, learnings that took real time to discover).\n\nIf something qualifies:\n- Follow the promotion lifecycle in `.claude/rules/memory_doctrine.md` (scratch → memory → rule → pointer).\n- Place new files in the correct subfolder (`feedback/`, `project/{meta,active,shipped}/`, `reference/{host,nvim-rpc,qt-pyside,agent-sdk}/`, etc.).\n- Add ONE bullet (≤150 chars) to `MEMORY.md` per new file, under the right section.\n- Keep `MEMORY.md` ≤200 lines.\n- If the committed work shipped a system that previously lived as a memory, shrink that memory file to a one-line pointer to the code (lifecycle step 4).\n\nReport one line to the user: either what you saved and why, or \"memory sweep: nothing new worth persisting.\"\n</system-reminder>"
  }
}
EOF

exit 0
