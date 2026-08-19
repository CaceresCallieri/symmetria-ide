---
name: invisible-glyph-literals
description: "A Nerd Font literal that has been flattened to \"\" is invisible in a diff, in a Read, and in review — only bytes or a render catch it"
metadata: 
  node_type: memory
  type: reference
  originSessionId: b8842b08-1d21-4ee7-b818-c807ca7ed268
  modified: 2026-08-19T15:50:57.693Z
---

# A lost glyph literal cannot be found by looking

A private-use-area glyph written as a LITERAL character in QML can be
flattened to an EMPTY STRING somewhere between an editor, a tool pipeline and
the file. When that happens the code still reads as correct at every layer a
human or an agent normally inspects:

- `text: ""` looks like a deliberately blank string, not like damage.
- A diff shows nothing unusual — there is no mojibake, no replacement
  character, no encoding warning.
- The `Read` tool renders it as `text: ""`, so an agent copying that line
  forward propagates the emptiness without ever holding the character.
- QML raises nothing. A zero-width `Text` simply draws nothing.

**Confirmed twice in this repo.** `Theme.glyph.worktree` was the first
(recorded in CLAUDE.md). The browser-ownership globe in
`qml/AgentThreadRail.qml` was the second: it shipped, was reviewed, and was
carried through a full delegate restructure while drawing nothing on every row
that owned a browser window. Verified at the byte level on 2026-08-19 —
`text: ""` is `74 65 78 74 3a 20 22 22`, with no bytes between the quotes.

**Why:** the failure has no symptom on the surfaces anyone checks. It is not
that the glyph renders wrong; it is that the glyph is *absent*, and absence in
a UI reads as "this indicator is not currently lit" — which is a legitimate
state for every glyph in this codebase, since they are all conditional
indicators. That is what let it survive review and a live debugging pass.

**How to apply:**

- Write glyphs as `\uXXXX` escapes in `Theme.glyph.*`, never as literal PUA
  characters, and bind the token. The escape is diff-visible and
  encoding-proof, and the token means one codepoint has one home.
- **When a glyph "does not appear", check the bytes before checking the font,
  the family, or the colour.** Read the file in Python and print the
  codepoints of the line; a shell pipeline can strip the character itself, so
  `grep | python` proves nothing — read the FILE.
- Verify a codepoint exists before trusting it, rather than trusting a Nerd
  Font cheat-sheet name:
  ```python
  from fontTools.ttLib import TTFont
  cmap = TTFont('/usr/share/fonts/TTF/CaskaydiaCoveNerdFont-Regular.ttf').getBestCmap()
  print(cmap.get(0xF0AC))   # -> 'fa-globe'
  ```
- Adding a glyph to a QML file that is not `Theme.qml` is itself the smell —
  a codepoint written at a call site has no token comment to warn the next
  reader, which is exactly how both losses happened.
