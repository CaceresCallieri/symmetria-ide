"""Hyprland IPC — window rules + workspace tracking for the external browser.

Why this exists: the agentic browser is real Google Chrome running as its own
process, so every window it opens is a top-level Hyprland surface. Left alone,
those windows appear on whatever workspace the user happens to be looking at —
the exact interruption the embedded QtWebEngine pool used to prevent. Hyprland
window rules put them back under control: each IDE instance launches Chrome
with a UNIQUE `--class`, and a matching rule pins every window of that class to
the IDE's own workspace, `silent` (no focus steal, no visible jump).

Two facts about Hyprland 0.56 are load-bearing here, both learned the hard way
when an earlier attempt at this silently did nothing:

1. **`windowrulev2` is deprecated and IGNORED.** It prints a deprecation notice
   and returns success. `hyprctl` exits 0 either way, so a caller checking the
   exit code concludes the rule was applied while no rule exists. That is why
   `apply_keyword` validates the response BODY (`ok`) and never the exit code.
2. **The matcher syntax changed** from `class:^(x)$` to `match:class ^(x)$`.
   The old form is rejected with `invalid field class:^(x)$: missing a value`.

This module is deliberately Qt-free: the pure rule/parse helpers are unit
testable without a Qt import, and the one background thread reports through a
plain callback. `chrome_host.ChromeHost` is the QObject that owns it and
marshals the callback onto the GUI thread.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

# `hyprctl` calls are local IPC over a Unix socket — they answer in single-digit
# milliseconds or not at all. A short timeout keeps a wedged compositor from
# stalling a GUI-thread call site.
_HYPRCTL_TIMEOUT_SEC = 2.0

# Events that can change which workspace a window sits on. We re-resolve our
# own window on these rather than trying to interpret each payload shape, since
# Hyprland's event field layouts differ per event and per version.
_MOVE_EVENTS = ("movewindowv2", "movewindow", "openwindow")

# Floor between two `hyprctl clients -j` resolves. Each is a subprocess; a
# window drag emits move events continuously, and without a floor the reader
# thread would spawn one per event.
_RESOLVE_DEBOUNCE_SEC = 0.15


def hyprland_available() -> bool:
    """True when we're running under a Hyprland compositor we can talk to.

    Everything in this module no-ops when False — the browser still works, its
    windows simply land wherever the compositor puts them. Chrome-under-another
    -compositor is a supported degradation, not an error.
    """
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))


# ----------------------------------------------------------------------
# Pure helpers (no subprocess, no compositor — unit tested directly)
# ----------------------------------------------------------------------


def workspace_rule_target(workspace_id: int, workspace_name: str = "") -> str:
    """Render a workspace as a window rule targets it.

    Hyprland accepts three spellings and they are not interchangeable:
    a bare id (`6`), a named workspace (`name:scratch`), and a special
    workspace (`special:magic`). Clients report both an id and a name, and for
    ordinary numbered workspaces the name is just the id as a string — so the
    name is only meaningful when it differs.
    """
    name = (workspace_name or "").strip()
    if name.startswith("special:"):
        return name
    if name and not name.lstrip("-").isdigit():
        return f"name:{name}"
    return str(workspace_id)


def build_workspace_rule(target: str, class_name: str) -> str:
    """The rule that pins `class_name`'s windows to `target`, silently.

    `silent` is the whole point: the window is placed on the target workspace
    WITHOUT following focus there, so an agent opening a browser never moves
    the user off what they are doing.
    """
    return f"workspace {target} silent, match:class ^({class_name})$"


def build_unset_rule(class_name: str) -> str:
    """The rule that releases `class_name` (IDE shutdown).

    Rules accumulate in the running compositor for the life of the session, so
    an IDE that exits without this leaves a rule pointing at a workspace for a
    class nothing will ever map again.
    """
    return f"workspace unset, match:class ^({class_name})$"


def parse_own_window(clients_json: str, pid: int) -> dict | None:
    """Find our own window in `hyprctl clients -j` output.

    Returns `{"address", "workspace_id", "workspace_name"}` or None when the
    window has not mapped yet (early startup) or the payload is unusable.
    Malformed JSON is a None, not a raise: this feeds a best-effort placement
    path, and no browser pinning is a far better outcome than a dead IDE.
    """
    try:
        clients = json.loads(clients_json)
    except (ValueError, TypeError):
        log.warning("hyprctl clients: unparseable JSON")
        return None
    if not isinstance(clients, list):
        return None
    for client in clients:
        if not isinstance(client, dict) or client.get("pid") != pid:
            continue
        workspace = client.get("workspace") or {}
        if not isinstance(workspace, dict):
            return None
        try:
            return {
                "workspace_id": int(workspace.get("id", 0)),
                "workspace_name": str(workspace.get("name", "")),
            }
        except (TypeError, ValueError):
            # A non-numeric id would otherwise raise out of the reader thread
            # or out of resolve_now() on the GUI thread, against a docstring
            # that promises None for anything unusable.
            log.warning("hyprctl clients: unusable workspace payload %r", workspace)
            return None
    return None


def parse_event_line(line: str) -> tuple[str, list[str]]:
    """Split a `.socket2` line (`EVENT>>arg,arg,arg`) into name + args.

    Returns `("", [])` for anything unrecognised, so callers can filter by name
    without guarding shape. Only the NAME has a consumer today — the watcher
    re-resolves our window by pid rather than parsing per-event field layouts,
    which differ by event and by Hyprland version. The args are returned
    anyway because filtering by them is the obvious optimisation if the
    re-resolve ever becomes too costly.
    """
    name, sep, payload = line.partition(">>")
    if not sep:
        return "", []
    return name.strip(), payload.split(",") if payload else []


def event_socket_path() -> str:
    """Path of Hyprland's event socket, or "" when not resolvable."""
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if not signature or not runtime:
        return ""
    return os.path.join(runtime, "hypr", signature, ".socket2.sock")


# ----------------------------------------------------------------------
# Compositor-touching calls
# ----------------------------------------------------------------------


def _run_hyprctl(args: list[str]) -> str | None:
    """Run `hyprctl <args>` and return stdout, or None on any failure."""
    try:
        result = subprocess.run(
            ["hyprctl", *args],
            capture_output=True,
            text=True,
            timeout=_HYPRCTL_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("hyprctl %s failed: %s", " ".join(args), exc)
        return None
    if result.returncode != 0:
        log.warning("hyprctl %s exited %d", " ".join(args), result.returncode)
        return None
    return result.stdout


def apply_keyword(rule: str) -> bool:
    """Install a runtime `windowrule`, verifying it was actually accepted.

    Returns True only when Hyprland answers exactly `ok`. This check is the
    reason the module exists in this shape: `hyprctl` exits 0 for a deprecated
    keyword and for a rejected matcher alike, so an exit-code check reports
    success while no rule was installed. Every failure mode we have hit —
    `windowrulev2 is deprecated`, `invalid field class:^(x)$: missing a value`
    — is visible ONLY in the response body.
    """
    if not hyprland_available():
        return False
    output = _run_hyprctl(["keyword", "windowrule", rule])
    if output is None:
        return False
    response = output.strip()
    if response != "ok":
        log.warning("hyprctl rejected windowrule %r: %s", rule, response or "(empty)")
        return False
    log.info("windowrule applied: %s", rule)
    return True


def own_window(pid: int | None = None) -> dict | None:
    """Resolve this process's Hyprland window (address + workspace)."""
    if not hyprland_available():
        return None
    output = _run_hyprctl(["clients", "-j"])
    if output is None:
        return None
    return parse_own_window(output, os.getpid() if pid is None else pid)


class WorkspaceWatcher:
    """Calls back when THIS process's window changes workspace.

    A window rule is evaluated when a window MAPS, so pinning Chrome once at
    startup is not enough: move the IDE to another workspace and every window
    opened afterwards would still be sent to the old one. This watcher lets the
    owner re-issue the rule (later rules win — verified against 0.56).

    Implementation note: rather than interpret each event's field layout (which
    varies by event and version), we re-resolve our own window on the handful
    of events that can move a window. That costs one `hyprctl clients -j` per
    such event — cheap, and immune to payload drift.

    The callback runs on the reader thread. The owner is responsible for
    marshaling to the GUI thread.
    """

    def __init__(self, on_workspace_changed: Callable[[int, str], None]) -> None:
        self._callback = on_workspace_changed
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._workspace_id: int = 0
        self._workspace_name: str = ""
        self._last_resolve: float = 0.0

    @property
    def workspace_id(self) -> int:
        """Last known workspace id; 0 before the first resolve."""
        return self._workspace_id

    @property
    def workspace_name(self) -> str:
        return self._workspace_name

    def resolve_now(self) -> tuple[int, str]:
        """Resolve (and cache) our current workspace synchronously."""
        window = own_window()
        if window:
            self._workspace_id = window["workspace_id"]
            self._workspace_name = window["workspace_name"]
        return self._workspace_id, self._workspace_name

    def start(self) -> None:
        """Begin watching. No-op off Hyprland, or if already running.

        The socket is created HERE, on the caller's thread, rather than inside
        `_run`: otherwise a `stop()` arriving before the reader thread got
        scheduled would find `_sock` still None, skip the shutdown, and leave
        the reader parked on `recv` forever.
        """
        if self._thread is not None or not hyprland_available():
            return
        path = event_socket_path()
        if not path:
            log.info("hyprland event socket unresolvable — workspace follow off")
            return
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(path)
        except OSError as exc:
            log.warning("hyprland event socket connect failed: %s", exc)
            return
        self._sock = sock
        self._stop.clear()  # allow restart after a stop()
        self.resolve_now()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="hypr-events"
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the reader to exit, unblock its recv, and let it finish."""
        self._stop.set()
        # Shutting the socket down is what actually unblocks the blocking
        # recv; setting the event alone would leave the thread parked until
        # the next event arrives. It is a daemon thread either way, so a
        # failure here costs nothing at exit.
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        # Join and clear so start() can run again — leaving `_thread` set made
        # any restart a silent no-op.
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _run(self) -> None:
        buffer = ""
        try:
            while not self._stop.is_set():
                try:
                    chunk = self._sock.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                # Drain the whole chunk before resolving: a window drag or a
                # burst of app launches arrives as many move events at once,
                # and resolving per line would fire a `hyprctl` subprocess for
                # each. Only the final state matters.
                interesting = False
                while "\n" in buffer:
                    line, _, buffer = buffer.partition("\n")
                    name, _args = parse_event_line(line)
                    interesting = interesting or name in _MOVE_EVENTS
                if interesting:
                    self._resolve_and_notify()
        finally:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _resolve_and_notify(self) -> None:
        """Re-resolve our window; call back only when the workspace changed."""
        now = time.monotonic()
        if now - self._last_resolve < _RESOLVE_DEBOUNCE_SEC:
            return
        self._last_resolve = now
        window = own_window()
        if not window:
            return
        if window["workspace_id"] == self._workspace_id:
            return
        self._workspace_id = window["workspace_id"]
        self._workspace_name = window["workspace_name"]
        try:
            self._callback(self._workspace_id, self._workspace_name)
        except Exception:
            # A raise here would kill the reader thread and silently end
            # workspace following for the rest of the session.
            log.exception("workspace-changed callback failed")
