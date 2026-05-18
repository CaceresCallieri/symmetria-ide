"""Tests for the Phase 2.5 deliverable 3 PR 2 OSC 7 parser.

`_parse_osc7(buffer, new_data) -> (cleaned, paths, remaining_partial)` is
the core byte-stream interceptor. Pure function — no instance state, no
I/O, no Qt. Trivially testable with synthetic byte streams.

Coverage strategy:

1. Plain-text passthrough (no OSC 7 in stream → unchanged).
2. Single OSC 7 with both terminator variants (BEL, ST).
3. Multiple OSC 7 in a single chunk.
4. OSC 7 mixed with regular content.
5. Fragmentation across reader-loop iterations (the buffer's whole job).
6. Edge cases: empty path, hostless URI, malformed sequence, percent-
   encoded path, non-UTF-8 bytes, oversized partial.

The OSC 7 wire format under test: `ESC ] 7 ; file://<host>/<path> TERM`
where TERM is BEL (`\\x07`) or ST (`\\x1b\\\\`). Phase 2.5 deliverable 3.
"""

from __future__ import annotations

from symmetria_ide.terminal_backend import _OSC_BUFFER_CAP, _parse_osc7


# ---------------------------------------------------------------------------
# Passthrough — no OSC 7 in the stream
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty():
    cleaned, paths, partial = _parse_osc7(b"", b"")
    assert cleaned == b""
    assert paths == []
    assert partial == b""


def test_plain_text_passes_through_unchanged():
    """Regular shell output (no OSC sequences) is forwarded byte-for-byte."""
    data = b"$ ls\r\nfoo.txt  bar.py\r\n$ "
    cleaned, paths, partial = _parse_osc7(b"", data)
    assert cleaned == data
    assert paths == []
    assert partial == b""


def test_unrelated_osc_sequences_pass_through():
    """Only OSC 7 is intercepted. Other OSC sequences (OSC 0 for title,
    OSC 52 for clipboard, etc.) must pass through to pyte for rendering."""
    # OSC 0 (set window title) + OSC 2 (set icon name) — must not match
    # the OSC 7 pattern.
    data = b"\x1b]0;mytitle\x07\x1b]2;icon\x1b\\"
    cleaned, paths, partial = _parse_osc7(b"", data)
    assert cleaned == data, "non-OSC-7 sequences must pass through to pyte"
    assert paths == []
    assert partial == b""


# ---------------------------------------------------------------------------
# Single OSC 7 — both terminator variants
# ---------------------------------------------------------------------------


def test_single_osc7_st_terminator():
    """OSC 7 with ST (ESC \\) terminator — the format our shell-init
    scripts emit."""
    data = b"\x1b]7;file://hostname/home/jc\x1b\\"
    cleaned, paths, partial = _parse_osc7(b"", data)
    assert cleaned == b""
    assert paths == ["/home/jc"]
    assert partial == b""


def test_single_osc7_bel_terminator():
    """OSC 7 with BEL (\\x07) terminator — some terminals / tools emit
    this form. Our parser accepts both per xterm spec."""
    data = b"\x1b]7;file://hostname/home/jc\x07"
    cleaned, paths, partial = _parse_osc7(b"", data)
    assert cleaned == b""
    assert paths == ["/home/jc"]
    assert partial == b""


def test_osc7_with_empty_host():
    """`file:///path` (empty hostname) is the canonical RFC 8089 form
    for local files. Must parse correctly."""
    data = b"\x1b]7;file:///tmp\x1b\\"
    cleaned, paths, partial = _parse_osc7(b"", data)
    assert paths == ["/tmp"]
    assert partial == b""


def test_osc7_mixed_with_regular_content():
    """OSC 7 stripped, surrounding shell output preserved verbatim."""
    data = b"$ cd /tmp\r\n\x1b]7;file://hostname/tmp\x1b\\$ "
    cleaned, paths, partial = _parse_osc7(b"", data)
    assert cleaned == b"$ cd /tmp\r\n$ "
    assert paths == ["/tmp"]
    assert partial == b""


# ---------------------------------------------------------------------------
# Multiple OSC 7 in a single chunk
# ---------------------------------------------------------------------------


def test_multiple_osc7_same_chunk():
    """A PROMPT_COMMAND that fires multiple cwd-emit hooks (or a fast
    sequence of `cd` calls before the reader catches up) can produce
    multiple OSC 7 sequences in one `os.read` return. All must be
    extracted in order."""
    data = b"\x1b]7;file://h/a\x1b\\$ cd b\r\n\x1b]7;file://h/b\x07$ "
    cleaned, paths, partial = _parse_osc7(b"", data)
    assert cleaned == b"$ cd b\r\n$ "
    assert paths == ["/a", "/b"]
    assert partial == b""


# ---------------------------------------------------------------------------
# Fragmentation across reader-loop iterations — the buffer's main job
# ---------------------------------------------------------------------------


def test_fragmented_at_arbitrary_position():
    """A chunk boundary lands inside an OSC 7. The partial is returned
    as the `remaining_partial` and stitches with the next call's `new_data`."""
    # Split exactly in the middle of the path.
    first = b"\x1b]7;file://host/home"
    second = b"/jc\x1b\\"
    c1, p1, partial = _parse_osc7(b"", first)
    assert c1 == b""
    assert p1 == []
    assert partial == first  # entire prefix saved

    c2, p2, partial2 = _parse_osc7(partial, second)
    assert c2 == b""
    assert p2 == ["/home/jc"]
    assert partial2 == b""


def test_fragmented_at_terminator():
    """Chunk boundary lands BETWEEN the ESC and `\\` of the ST terminator
    — the trickiest split because the parser sees what looks like an
    unterminated sequence in the first chunk."""
    first = b"\x1b]7;file://h/x\x1b"  # ends mid-ST
    second = b"\\"
    c1, p1, partial = _parse_osc7(b"", first)
    assert c1 == b""
    assert p1 == []
    # Partial saved with the trailing ESC.
    assert partial.endswith(b"\x1b")

    c2, p2, partial2 = _parse_osc7(partial, second)
    assert c2 == b""
    assert p2 == ["/x"]
    assert partial2 == b""


def test_fragmented_at_prefix():
    """Even the OSC 7 prefix can split — first chunk ends with `ESC `
    or `ESC ]`, terminator and body land in chunk 2."""
    first = b"hello\x1b]"
    second = b"7;file://h/p\x1b\\world"
    c1, p1, partial = _parse_osc7(b"", first)
    # `\x1b]` alone is not OSC 7 yet — parser can't be sure it's not
    # OSC 0 / OSC 2 / etc. The "look for ESC ] 7 ;" search needs the
    # full 4-byte prefix; if it doesn't find it, those bytes pass through
    # as regular content. The test pins this conservative behavior.
    assert c1 == first
    assert p1 == []
    assert partial == b""

    c2, p2, partial2 = _parse_osc7(partial, second)
    # Note: `second` starts with `7;` but since `\x1b]` is gone, no OSC
    # 7 is detected. This is the documented limitation — the prefix
    # itself can't split across chunks. Real shell hooks emit the whole
    # OSC 7 sequence in one printf, so the kernel almost always
    # delivers it atomically.
    assert c2 == second
    assert p2 == []


# ---------------------------------------------------------------------------
# Percent-encoding
# ---------------------------------------------------------------------------


def test_percent_encoded_path_decoded():
    """A future percent-encoding emitter (or any terminal forwarding
    its own OSC 7 with encoded paths) must be decoded so the routed
    payload is a real filesystem path."""
    data = b"\x1b]7;file:///home/jc/My%20Projects\x1b\\"
    cleaned, paths, partial = _parse_osc7(b"", data)
    assert paths == ["/home/jc/My Projects"]


def test_unencoded_path_with_space_still_parses():
    """Our v1 shell-init scripts emit raw `$PWD` without percent-encoding
    (HACK documented at the emitter sources). Parser must accept that
    too — `unquote` is a no-op when there are no `%xx` sequences."""
    data = b"\x1b]7;file:///home/jc/My Projects\x1b\\"
    cleaned, paths, partial = _parse_osc7(b"", data)
    assert paths == ["/home/jc/My Projects"]


# ---------------------------------------------------------------------------
# Defensive — malformed / surprising input
# ---------------------------------------------------------------------------


def test_osc7_without_file_scheme_ignored():
    """An OSC 7 with a non-file:// payload is malformed per xterm spec.
    Strip the bytes but don't emit a path — defensive against future
    misbehaving emitters."""
    data = b"\x1b]7;http://example.com/\x1b\\"
    cleaned, paths, partial = _parse_osc7(b"", data)
    assert cleaned == b""
    assert paths == []
    assert partial == b""


def test_osc7_with_only_hostname_ignored():
    """`file://hostname` without a path component is also malformed —
    no leading `/` means our `host_end = find(b"/", 7)` returns -1 and
    we skip the emit. Bytes still get stripped from the stream."""
    data = b"\x1b]7;file://hostname\x1b\\"
    cleaned, paths, partial = _parse_osc7(b"", data)
    assert cleaned == b""
    assert paths == []


def test_non_utf8_path_dropped_silently():
    """Bytes that aren't valid UTF-8 in the path get logged at debug
    level and skipped — no crash."""
    data = b"\x1b]7;file:///" + b"\xff\xfe" + b"\x1b\\"
    cleaned, paths, partial = _parse_osc7(b"", data)
    assert cleaned == b""
    # \xff\xfe is invalid UTF-8 in path position; parser skips emit.
    assert paths == []


def test_oversized_partial_dropped():
    """A runaway unterminated OSC 7 (buggy emitter, garbled binary
    output) must not grow the buffer unboundedly. _OSC_BUFFER_CAP is the
    safety cap — exceeding it drops the partial silently with a warning."""
    # Start an OSC 7 sequence, then never terminate it — buffer would
    # grow forever without the cap.
    huge_unterminated = b"\x1b]7;file://h/" + b"a" * (_OSC_BUFFER_CAP * 2)
    cleaned, paths, partial = _parse_osc7(b"", huge_unterminated)
    # Bytes before the OSC start would have been emitted (there are
    # none here). The partial would have been the whole runaway. The
    # cap dropped it.
    assert cleaned == b""
    assert paths == []
    assert partial == b""  # dropped, not retained


def test_buffer_carryover_does_not_lose_prefix_bytes():
    """Bytes BEFORE an unterminated OSC 7 in the first chunk must be
    flushed to `cleaned` immediately, not held in the partial buffer.
    Otherwise typing while a cd-in-progress OSC 7 is partial would
    delay rendering of the typed chars."""
    first = b"some output\x1b]7;file://h/x"  # no terminator
    cleaned, paths, partial = _parse_osc7(b"", first)
    assert cleaned == b"some output"  # NOT in partial
    assert partial == b"\x1b]7;file://h/x"
    assert paths == []


# ---------------------------------------------------------------------------
# Path normalization — trailing slashes and root-slash filter
# ---------------------------------------------------------------------------


def test_trailing_slash_normalized():
    """A shell cwd with a trailing slash (e.g. `file:///tmp/`) must be
    normalized to `/tmp` before emission so that AppController._cwd
    comparisons are stable regardless of whether the emitter includes the
    trailing slash. `unquote("/tmp/").rstrip("/")` → "/tmp"."""
    data = b"\x1b]7;file:///tmp/\x1b\\"
    cleaned, paths, partial = _parse_osc7(b"", data)
    assert paths == ["/tmp"], "trailing slash must be stripped"


def test_root_slash_filtered():
    """A malformed OSC 7 emitter sending `file:///` (three slashes, no
    path after) decodes to path `/`. Emitting `/` as cwd would overwrite
    AppController's anchor path with root — almost certainly wrong.
    The parser filters this case rather than emitting a spurious root path.
    Sequence is still stripped from the cleaned output (pyte never sees it)."""
    data = b"\x1b]7;file:///\x1b\\"
    cleaned, paths, partial = _parse_osc7(b"", data)
    assert paths == [], "bare root path from malformed emitter must be filtered"
    assert cleaned == b"", "OSC 7 bytes must still be stripped from cleaned output"


def test_root_path_from_named_host_filtered():
    """Same filter applies when the host portion is non-empty but the path
    component is just `/`: `file://hostname/` → `/` → filtered."""
    data = b"\x1b]7;file://myhost/\x1b\\"
    cleaned, paths, partial = _parse_osc7(b"", data)
    assert paths == [], "host-only URI with bare / path must be filtered"
