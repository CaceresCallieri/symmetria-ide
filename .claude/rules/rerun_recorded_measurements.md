---
name: rerun-recorded-measurements
description: A recorded "we measured this and it is fine" is a claim to re-run, not a fact — especially when it also tells you not to build the fix
---

When a symptom contradicts something this repo has written down as measured,
**re-run the measurement before believing the note**. Recorded measurements are
point-in-time observations of a system that has moved since; a note saying "we
checked, it is fine" is evidence, not a verdict.

Treat a recorded measurement as **most** suspect when it comes with an
instruction not to build something ("this is free", "do not add machinery for
it"). That combination is what turns a stale number into lost hours: it does not
merely fail to help, it actively points the next investigation away from the
real layer.

**Why:** CLAUDE.md carried "with the IDE on an inactive workspace and the surface
hidden four different ways, a page held 61–62 rAF ticks/s and
`Page.captureScreenshot` stayed at ~60ms. Hiding the browser surface is free; do
not add machinery to 'keep it alive'." Measured again on 2026-07-28 in that same
configuration: **0 rAF ticks in 1007ms**, and screenshots that never returned.
The nested client deadlocks permanently when the host stops rendering, and the
fix was precisely the machinery the note forbade. The re-diagnosis started by
doubting the compositor plugin — the layer the note had cleared — because the
note said this layer was already checked.

Two more of this session's confident conclusions collapsed under a controlled
re-run: "the Chrome crash is load-sensitive" (reproduced load 19.6 synthetically,
no failure) and "`--disable-gpu` does not prevent it" (confounded by an
uncontrolled memory variable). Being wrong here is ordinary, not exceptional —
which is the argument for re-running rather than for distrusting any one author.

**How to apply:**

- Symptom contradicts a recorded measurement → re-run the measurement first.
  It is nearly always cheaper than the investigation you would otherwise start.
- When it does not reproduce, **correct the record explicitly** — say what was
  measured before, what it says now, and that the old conclusion is retracted.
  Silently overwriting it loses the reason the next reader should stay
  suspicious.
- When recording a measurement, give it the date, the configuration and the
  method, so re-running is possible at all. `.claude/memory/reference/qt-pyside/
  nested_compositor_frame_starvation.md` is the shape to copy.
- Do not generalise this into distrusting the repo's documentation. It applies
  when a SYMPTOM disagrees — not as a reason to re-derive everything.
