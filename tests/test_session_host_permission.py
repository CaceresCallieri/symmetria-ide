"""Unit tests for `SessionHost`'s wire-protocol write path — sidecar variant.

Replaces the prior `test_session_host_send.py` whose envelope assertions
matched the retired `claude -p` shape. The sidecar's wire protocol
(documented in `sidecar/src/protocol.ts` as the `InboundCommand` union)
uses two top-level command types:

    {"type": "user_message", "content": "..."}
    {"type": "permission_response", "request_id": "<uuid>",
     "behavior": "allow" | "deny"}

These tests exercise the serialization + lock + flush path without
spawning a real subprocess — `_proc` is swapped for a `_FakeProc`
that captures stdin writes. Same hermetic pattern as the prior
test file.

A live sidecar smoke test (piping a real claude SDK turn end-to-end)
would require an authenticated user environment and is intentionally
out of scope here — the SDK doesn't ship a stub mode and any such
test would charge tokens. Manual verification belongs in the
`docs/dev-workflow.md` "Phase 2 placeholder spike" section.
"""

from __future__ import annotations

import json

from symmetria_ide.session_host import SessionHost


class _FakeStdin:
    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.flush_count = 0
        self.raise_on_write: BaseException | None = None

    def write(self, s: str) -> int:
        if self.raise_on_write is not None:
            raise self.raise_on_write
        self.chunks.append(s)
        return len(s)

    def flush(self) -> None:
        self.flush_count += 1


class _FakeProc:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()


def _decode_one(fake: _FakeProc) -> dict:
    """Decode the single accumulated JSONL line into a dict."""
    line = "".join(fake.stdin.chunks).rstrip("\n")
    return json.loads(line)


# ---------------------------------------------------------------------------
# user_message envelope — replaces the retired stream-json `user` shape
# ---------------------------------------------------------------------------


def test_send_user_message_writes_user_message_command():
    """Verify the new sidecar `user_message` envelope shape.

    The sidecar wraps this in an SDKUserMessage internally; our wire
    contract is the flat {type, content} pair. The retired shape
    ({type:"user", message:{role,content}}) is no longer accepted by
    the sidecar — that translation is now the SDK's job.
    """
    host = SessionHost()
    fake = _FakeProc()
    host._proc = fake  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]

    host.send_user_message("hello sir")

    assert fake.stdin.chunks, "expected a write to stdin"
    assert fake.stdin.flush_count == 1, "flush must follow every write"
    assert _decode_one(fake) == {
        "type": "user_message",
        "content": "hello sir",
    }


def test_send_user_message_preserves_unicode():
    """Non-ASCII content must round-trip as UTF-8 inside the JSON."""
    host = SessionHost()
    fake = _FakeProc()
    host._proc = fake  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]

    host.send_user_message("¡hola! 🎉 — é")

    payload = _decode_one(fake)
    assert payload["content"] == "¡hola! 🎉 — é"


def test_send_user_message_without_subprocess_is_a_noop():
    """No subprocess = silent drop; callers shouldn't need is_running branching."""
    host = SessionHost()
    # Default `_proc` is None — nothing to write to.
    host.send_user_message("orphan turn")  # must not raise


def test_send_user_message_tolerates_broken_pipe():
    """If the sidecar vanished, write/flush surfaces are logged, not raised."""
    host = SessionHost()
    fake = _FakeProc()
    fake.stdin.raise_on_write = BrokenPipeError()
    host._proc = fake  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]

    # Should log the exception via `log.exception` but NOT propagate.
    host.send_user_message("after the crash")

    # flush must NOT be called when write already raised — the exception
    # exits the try-block before reaching the flush line.
    assert fake.stdin.flush_count == 0, "flush must not be called after a failed write"


# ---------------------------------------------------------------------------
# permission_response envelope — new write path for canUseTool replies
# ---------------------------------------------------------------------------


def test_send_permission_response_allow_writes_envelope_with_request_id():
    """Verify the canonical allow envelope shape."""
    host = SessionHost()
    fake = _FakeProc()
    host._proc = fake  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]

    host.send_permission_response("req-abc", "allow")

    assert fake.stdin.flush_count == 1
    assert _decode_one(fake) == {
        "type": "permission_response",
        "request_id": "req-abc",
        "behavior": "allow",
    }


def test_send_permission_response_deny_writes_envelope_with_request_id():
    """Deny shape is the same envelope with behavior switched."""
    host = SessionHost()
    fake = _FakeProc()
    host._proc = fake  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]

    host.send_permission_response("req-xyz", "deny")

    assert _decode_one(fake) == {
        "type": "permission_response",
        "request_id": "req-xyz",
        "behavior": "deny",
    }


def test_send_permission_response_rejects_invalid_behavior():
    """Anything other than 'allow'/'deny' is silently dropped — no write,
    no exception. Keeps the QML invocation surface tolerant."""
    host = SessionHost()
    fake = _FakeProc()
    host._proc = fake  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]

    host.send_permission_response("req-abc", "maybe")
    host.send_permission_response("req-abc", "")
    host.send_permission_response("req-abc", "ALLOW")  # case-sensitive

    assert fake.stdin.chunks == []
    assert fake.stdin.flush_count == 0


def test_send_permission_response_without_subprocess_is_a_noop():
    """Same defensive contract as send_user_message."""
    host = SessionHost()
    host.send_permission_response("orphan-id", "allow")  # must not raise


# ---------------------------------------------------------------------------
# set_permission_mode envelope — Shift+Tab cycle dispatch
# ---------------------------------------------------------------------------
#
# These tests cover the third inbound envelope the sidecar accepts: the
# `set_permission_mode` command emitted by AppController.cycle_permission_mode
# in response to Shift+Tab. The shape is deliberately small (just `mode`)
# because the sidecar replies with a `permission_mode_changed` echo on
# success, so there's no need for the inbound envelope to carry an ACK token.


def test_send_set_permission_mode_writes_envelope_for_each_valid_mode():
    """All four valid modes must serialise to the canonical envelope shape.

    Iterating in cycle order (default → acceptEdits → bypassPermissions →
    plan) doubles as a regression guard against accidental case-mangling
    or string drift between the controller and the host (the controller's
    `_PERMISSION_MODES` tuple is the source of truth; the host's validation
    set must stay in sync).
    """
    for mode in ("default", "acceptEdits", "bypassPermissions", "plan"):
        host = SessionHost()
        fake = _FakeProc()
        host._proc = fake  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]

        host.send_set_permission_mode(mode)

        assert fake.stdin.flush_count == 1, f"flush missing for mode={mode}"
        assert _decode_one(fake) == {
            "type": "set_permission_mode",
            "mode": mode,
        }


def test_send_set_permission_mode_rejects_invalid_mode():
    """Anything outside the four-mode union is silently dropped.

    Defensive guard — even though the controller's cycle slot only ever
    emits valid modes, a future caller (or a malformed direct invocation
    from QML) shouldn't crash the host or write garbage to the sidecar.
    Mirrors the `send_permission_response` invalid-behavior pattern.
    """
    host = SessionHost()
    fake = _FakeProc()
    host._proc = fake  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]

    host.send_set_permission_mode("ACCEPT_EDITS")  # case-sensitive
    host.send_set_permission_mode("auto")  # SDK mode we deliberately omit
    host.send_set_permission_mode("dontAsk")  # SDK mode we deliberately omit
    host.send_set_permission_mode("")
    host.send_set_permission_mode("garbage")

    assert fake.stdin.chunks == []
    assert fake.stdin.flush_count == 0


def test_send_set_permission_mode_without_subprocess_is_a_noop():
    """Same defensive contract as the other two send_* methods."""
    host = SessionHost()
    host.send_set_permission_mode("plan")  # must not raise


# ---------------------------------------------------------------------------
# Pre-warm path — `start("")` spawns the subprocess without sending an
# initial user message
# ---------------------------------------------------------------------------
#
# The pre-warm contract is what makes the permission-mode pill +
# Shift+Tab cycling work the moment the agent pane is reachable, instead
# of silently dropping `set_permission_mode` writes on a non-existent
# stdin until the user sends their first message. AppController.start()
# calls `session_host.start("")` unconditionally; if a future refactor
# changes `start("")` to either error out or send an empty user_message
# envelope, the user-visible Shift+Tab silent-drop bug returns.


class _EmptyStream:
    """`readline()` returns "" immediately so worker threads exit at EOF."""

    def readline(self) -> str:
        return ""

    def __iter__(self):
        # `_run_stderr_loop` iterates the stream directly (`for line in proc.stderr`)
        # — yielding nothing closes the iterator and the loop falls through.
        return iter(())


class _FakeProcWithStreams:
    """Subprocess stand-in shaped for the pre-warm + stop lifecycle.

    Exposes the surface `start()` and `stop()` touch:
      stdin   — `_FakeStdin` (captures any write attempts)
      stdout  — `_EmptyStream` (worker reads "" → loop exits cleanly)
      stderr  — `_EmptyStream` (stderr worker iterates → empty → exits)
      terminate / wait / kill — no-ops so `host.stop()` doesn't blow up
    """

    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _EmptyStream()
        self.stderr = _EmptyStream()
        self.terminate_calls = 0

    def terminate(self) -> None:
        self.terminate_calls += 1

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        return 0

    def kill(self) -> None:
        pass


def _install_pre_warm_fakes(monkeypatch, fake_proc: _FakeProcWithStreams) -> None:
    """Wire `Popen` + `_sidecar_dist_path` so `start()` runs without a real subprocess.

    Module-level monkeypatch on `symmetria_ide.session_host.subprocess.Popen`
    intercepts the spawn; `_sidecar_dist_path` returns a path that exists
    so the dist-check guard at the top of `start()` doesn't bail early.
    """
    import symmetria_ide.session_host as session_host_module

    def fake_popen(*_args, **_kwargs):
        return fake_proc

    monkeypatch.setattr(session_host_module.subprocess, "Popen", fake_popen)
    # Use `__file__` itself as the "exists" target — no temp-file plumbing.
    monkeypatch.setattr(
        session_host_module,
        "_sidecar_dist_path",
        lambda: __import__("pathlib").Path(__file__),
    )


def test_start_with_empty_prompt_does_not_send_user_message(monkeypatch):
    """Pre-warm contract — `start("")` spawns but writes nothing to stdin.

    This is the regression guard for the user-visible bug. If `start("")`
    accidentally sends `{"type":"user_message","content":""}`, the SDK
    receives an empty turn and may either (a) hallucinate context or
    (b) error. Either way, the pane shows confusing output the moment
    the IDE launches.
    """
    host = SessionHost()
    fake = _FakeProcWithStreams()
    _install_pre_warm_fakes(monkeypatch, fake)

    try:
        host.start("")
        # `_proc is not None` is the spawn proof; `is_running` is not
        # asserted here because the fake stdout/stderr streams EOF
        # immediately, which causes the worker threads to set
        # `_stop_event` in their finally blocks before the test reaches
        # the assertion. In production the real stdout blocks on the
        # SDK's first event, so `is_running` stays True — the artificial
        # immediate EOF is a test-only artifact, not a contract change.
        assert host._proc is not None  # pyright: ignore[reportPrivateUsage]
        assert fake.stdin.chunks == [], (
            "pre-warm must not write any inbound envelope to stdin"
        )
        assert fake.stdin.flush_count == 0
    finally:
        host.stop()


def test_start_with_nonempty_prompt_still_sends_user_message(monkeypatch):
    """Backward-compat — non-empty prompt path keeps the initial write.

    Locks in that the empty-prompt change didn't accidentally regress
    the `SYMMETRIA_IDE_AGENT_PROMPT` headless-smoke path or the future
    cold-start composer flow.
    """
    host = SessionHost()
    fake = _FakeProcWithStreams()
    _install_pre_warm_fakes(monkeypatch, fake)

    try:
        host.start("hello sir")
        assert host._proc is not None  # pyright: ignore[reportPrivateUsage]
        assert fake.stdin.flush_count == 1
        assert _decode_one(fake) == {  # type: ignore[arg-type]
            "type": "user_message",
            "content": "hello sir",
        }
    finally:
        host.stop()


def test_send_set_permission_mode_after_prewarm_writes_envelope(monkeypatch):
    """Regression guard — the pre-warm makes mode-cycling work immediately.

    This is the test that, had it existed before, would have caught the
    Shift+Tab silent-drop bug. After pre-warm, `_proc is not None`, so
    `_write_command` proceeds instead of taking the silent-drop branch.
    """
    host = SessionHost()
    fake = _FakeProcWithStreams()
    _install_pre_warm_fakes(monkeypatch, fake)

    try:
        host.start("")
        host.send_set_permission_mode("acceptEdits")

        assert fake.stdin.flush_count == 1
        assert _decode_one(fake) == {  # type: ignore[arg-type]
            "type": "set_permission_mode",
            "mode": "acceptEdits",
        }
    finally:
        host.stop()
