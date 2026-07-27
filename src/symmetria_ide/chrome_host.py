"""The IDE-owned Google Chrome process: profile, window pinning, CDP.

This replaces the embedded QtWebEngine pool. The trade it makes: we give up
rendering the browser inside our own window, and in exchange the agentic
browser becomes a browser that actually works — `Target.createTarget` (the
`new_page` every chrome-devtools-mcp workflow starts from) is unsupported on
QtWebEngine, screenshots stall when the IDE is off-workspace, and there are no
extensions and no real logins. Containment moves from "embedded item" to
"window pinned by a Hyprland rule" (hyprland_ipc.py).

Three constraints shape everything here, and all three trace back to one fact:
**Chrome is a singleton per `--user-data-dir`.**

1. **One Chrome process per IDE instance.** A second IDE pointed at the same
   profile would not start its own process — it would hand the request to the
   first one over IPC, and the resulting window would inherit the FIRST IDE's
   `--class`, landing on the wrong workspace. Verified live.
2. **Therefore one profile per project.** Not a preference; the process
   identity chain is `profile → process → class → workspace`.
3. **Therefore logins are seeded, not shared.** A template profile
   (`<data>/browser/_template`) is where the user logs into the dashboards
   once; each project profile is cloned from it on first use. Cookies survive
   the copy because Chrome's encryption key lives in the OS keyring, shared
   across profiles of the same user — but `Local State` must come along, since
   it carries the wrapped key reference.

The same singleton fact will hold for the nested-compositor backend: a Chrome
process connects to exactly one Wayland display. So this profile model is not
a stopgap for the pinned approach — it is the model either way.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import threading

from PySide6.QtCore import QObject, Qt, Signal, Slot

from . import hyprland_ipc
from .agent_bridge import emit_gc_safe
from .cdp_client import CdpClient

log = logging.getLogger(__name__)

# Chrome package names, most specific first.
_CHROME_EXECUTABLES = ("google-chrome-stable", "google-chrome", "chromium")

# Files copied from the template into a fresh project profile. A seeded profile
# can be hundreds of MB (cache, GPU shaders, site data); these carry the logins
# and nothing else. `Local State` is profile-ROOT, not inside Default, and is
# non-optional: it holds the OS-crypt wrapped key without which every copied
# cookie decrypts to garbage.
_TEMPLATE_ROOT_FILES = ("Local State",)
_TEMPLATE_PROFILE_FILES = (
    "Cookies",
    "Login Data",
    "Login Data For Account",
    "Preferences",
    "Secure Preferences",
    "Web Data",
)
_TEMPLATE_PROFILE_DIRS = ("Local Storage", "IndexedDB")

_SLUG_SAFE = re.compile(r"[^a-zA-Z0-9._-]+")


def data_home() -> str:
    """`$XDG_DATA_HOME/symmetria-ide` (never writes outside XDG — §9)."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "symmetria-ide")


def browser_root() -> str:
    return os.path.join(data_home(), "browser")


def template_profile_dir() -> str:
    """Where the user logs in once. Cloned into every project profile."""
    return os.path.join(browser_root(), "_template")


def project_slug(project_root: str) -> str:
    """A filesystem-safe, collision-resistant name for a project's profile.

    Basename for legibility (so a human can tell whose profile is whose) plus
    a short path digest, because two checkouts of the same repo — a worktree,
    or dev vs stable — share a basename but must not share a profile: they run
    as separate IDE instances, and a shared profile would collapse them onto
    one Chrome process.
    """
    root = (project_root or "").rstrip("/") or "default"
    base = _SLUG_SAFE.sub("-", os.path.basename(root)) or "project"
    digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


def profile_dir_for(project_root: str) -> str:
    return os.path.join(browser_root(), project_slug(project_root))


def chrome_executable() -> str:
    """The Chrome binary to launch, or "" when there isn't a usable one.

    `SYMMETRIA_IDE_CHROME_BIN` overrides the PATH search — for a non-standard
    install, and as the suite's hard off switch (tests/conftest.py points it at
    a nonexistent path so no test can ever spawn a real browser). When the
    override is set but unusable we return "" rather than falling back: a typo
    silently launching some other browser is worse than a logged no-browser.
    """
    override = os.environ.get("SYMMETRIA_IDE_CHROME_BIN")
    if override is not None:
        if os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        log.warning("SYMMETRIA_IDE_CHROME_BIN=%r is not executable", override)
        return ""
    for name in _CHROME_EXECUTABLES:
        path = shutil.which(name)
        if path:
            return path
    return ""


def window_class_for(pid: int) -> str:
    """The unique Wayland `app_id` this IDE's Chrome windows carry.

    Per-PID rather than per-project: the window rule must address ONE running
    IDE, and two IDEs can legitimately have the same project open (dev and
    stable, or two worktrees).
    """
    return f"symmetria-browser-{pid}"


def seed_profile_from_template(profile: str, template: str) -> bool:
    """Clone the login-bearing subset of `template` into a fresh `profile`.

    Returns True when a seed actually happened. No template, or a profile that
    already exists, is a normal no-op — the user simply logs in inside the
    project, and nothing is ever overwritten (an overwrite would silently
    destroy sessions established in that project).
    """
    if os.path.exists(profile):
        return False
    if not os.path.isdir(template):
        log.info("no browser template at %s — starting with a blank profile", template)
        return False
    # Seed into a staging dir and rename into place, so a failure mid-copy
    # leaves NOTHING behind. Copying straight into `profile` would leave a
    # half-populated directory that this function's own existence check then
    # treats as "already seeded" forever — the user would be silently logged
    # out of every dashboard, permanently, from one transient OSError.
    staging = f"{profile}.seeding"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        os.makedirs(os.path.join(staging, "Default"), exist_ok=True)
        for name in _TEMPLATE_ROOT_FILES:
            _copy_if_present(os.path.join(template, name), os.path.join(staging, name))
        for name in _TEMPLATE_PROFILE_FILES:
            _copy_if_present(
                os.path.join(template, "Default", name),
                os.path.join(staging, "Default", name),
            )
        for name in _TEMPLATE_PROFILE_DIRS:
            source = os.path.join(template, "Default", name)
            if os.path.isdir(source):
                shutil.copytree(
                    source, os.path.join(staging, "Default", name), dirs_exist_ok=True
                )
        os.rename(staging, profile)
    except OSError:
        log.exception("browser profile seed from %s failed", template)
        shutil.rmtree(staging, ignore_errors=True)
        return False
    log.info("seeded browser profile %s from template", profile)
    return True


def _copy_if_present(source: str, destination: str) -> None:
    if os.path.isfile(source):
        shutil.copy2(source, destination)


def build_chrome_argv(
    executable: str, window_class: str, profile: str, cdp_port: int, url: str = ""
) -> list[str]:
    """Chrome's launch argv.

    Deliberately NOT `--app=<url>`: the user wants the full browser — tabs,
    omnibox, extensions — because this browser doubles as something they show
    to other people, not just an agent's viewport.

    `url` is load-bearing on a COLD start. Chrome always opens a window when it
    launches, so starting it bare and then asking for a window yields TWO: a
    stray `chrome://newtab/` plus the one that was wanted. The registry adopts
    whichever target CDP discovers first, which is the stray — leaving the real
    window unattributed and un-closable. Launching straight at the target url
    makes the startup window BE the requested window. (Observed live, not
    theorised: the newtab target arrived first every time.)
    """
    argv = [
        executable,
        f"--class={window_class}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if cdp_port > 0:
        argv.append(f"--remote-debugging-port={cdp_port}")
    if url:
        argv.append(url)
    return argv


class ChromeHost(QObject):
    """Owns the Chrome process, its window rule, and the CDP session."""

    #: (target_id, url, title) — a window appeared or changed.
    windowUpdated = Signal(str, str, str)
    #: (target_id) — a window went away (user closed it, or we did).
    windowGone = Signal(str)
    #: Chrome itself is gone (quit, killed, crashed) — every window with it.
    #: The owner must drop its whole registry: otherwise browser_list_windows
    #: keeps reporting windows that no longer exist and agents act on the lie.
    browserGone = Signal()

    # Emitted from the Hyprland reader thread; queued onto the GUI thread.
    _workspaceMoved = Signal(int, str)

    def __init__(self, project_root: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._project_root = project_root
        self._window_class = window_class_for(os.getpid())
        self._profile = profile_dir_for(project_root)
        self._proc: subprocess.Popen | None = None
        self._pinned = False
        self._stopping = False
        self._cdp = CdpClient(self)
        self._cdp.targetUpdated.connect(self.windowUpdated)
        self._cdp.targetGone.connect(self.windowGone)
        self._cdp.disconnected.connect(self._on_cdp_disconnected)
        self._watcher = hyprland_ipc.WorkspaceWatcher(self._on_workspace_moved_thread)
        # queued: hyprland event reader thread -> ChromeHost GUI thread
        self._workspaceMoved.connect(
            self._on_workspace_moved, Qt.ConnectionType.QueuedConnection
        )

    @property
    def window_class(self) -> str:
        return self._window_class

    @property
    def profile(self) -> str:
        return self._profile

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def cdp_port(self) -> int:
        """The port reserved at startup in `app.run()`.

        Read from the environment rather than passed in: it is the single
        source of truth that `browser_mcp` also reads when building each
        agent's chrome-devtools-mcp entry, so a second copy could drift.
        """
        try:
            return int(os.environ.get("SYMMETRIA_IDE_CDP_PORT", "") or 0)
        except ValueError:
            return 0

    # -- lifecycle ------------------------------------------------------

    def ensure_running(self, initial_url: str = "") -> str:
        """Start Chrome if needed. Returns "" on success, else an error code.

        Lazy on purpose: a project that never opens a browser never pays for
        one. Called on every window open, so it doubles as the reconnect path
        after a user-killed Chrome.

        `initial_url` is used only on a cold start — see `build_chrome_argv`
        for why the startup window must be the requested one.
        """
        if self.is_running:
            # Re-attempt a pin that never took (the IDE window may not have
            # been mapped yet at spawn time). Without this the deferral is
            # permanent and every window of the session escapes the rule.
            if not self._pinned:
                self._apply_pin_rule()
            self._cdp.connect_to(self.cdp_port())
            return ""
        executable = chrome_executable()
        if not executable:
            log.warning("no Chrome on PATH — agent browser tools unavailable")
            return "chrome-not-installed"
        seed_profile_from_template(self._profile, template_profile_dir())
        try:
            os.makedirs(self._profile, exist_ok=True)
        except OSError:
            log.exception("could not create browser profile dir %s", self._profile)
            return "profile-unavailable"
        # Pin BEFORE spawning: the rule is evaluated when a window maps, so
        # installing it afterwards would let the very first window land on
        # whatever workspace the user is looking at — the interruption this
        # whole mechanism exists to prevent.
        self._apply_pin_rule()
        argv = build_chrome_argv(
            executable, self._window_class, self._profile, self.cdp_port(), initial_url
        )
        try:
            self._proc = subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except OSError:
            log.exception("failed to spawn Chrome")
            return "chrome-spawn-failed"
        log.info(
            "Chrome spawned (class=%s, profile=%s)", self._window_class, self._profile
        )
        # Reap in the background so a user-closed Chrome doesn't linger as a
        # zombie for the life of the IDE.
        threading.Thread(
            target=self._proc.wait, daemon=True, name="chrome-reap"
        ).start()
        self._watcher.start()
        self._cdp.connect_to(self.cdp_port())
        return ""

    @Slot()
    def _on_cdp_disconnected(self) -> None:
        """An established CDP session dropped — Chrome is gone.

        Only fires for a session that WAS attached (see `CdpClient`), so a
        failed initial attach can't be mistaken for Chrome dying.
        """
        if self._stopping:
            return  # our own teardown closed the socket; the owner knows
        log.info("Chrome disconnected — dropping the window registry")
        self._pinned = False
        self.browserGone.emit()

    def stop(self) -> None:
        """Tear down at IDE shutdown: CDP, watcher, rule, process."""
        self._stopping = True
        self._cdp.close()
        self._watcher.stop()
        if hyprland_ipc.hyprland_available():
            hyprland_ipc.apply_keyword(
                hyprland_ipc.build_unset_rule(self._window_class)
            )
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                log.exception("Chrome terminate failed")

    # -- windows --------------------------------------------------------

    def open_window(self, url: str, callback) -> str:
        """Ask Chrome for a new window at `url`.

        Returns "" when the request was dispatched (the `callback` receives the
        CDP result carrying `targetId`), or an error code when it could not be.

        The CDP attach is asynchronous, so the FIRST open after a cold Chrome
        start always races it. Rather than queue commands behind the socket —
        which hides the failure and makes the agent wait on nothing — we let
        Chrome itself open the url: as its startup window when cold, or via a
        `--new-window` handoff when warm-but-unattached. Either way the window
        lands correctly (the pin rule is a compositor-side match on class, not
        something we drive) and joins the registry through target discovery
        once CDP attaches.
        """
        target_url = url or "about:blank"
        cold_start = not self.is_running
        error = self.ensure_running(initial_url=target_url if cold_start else "")
        if error:
            return error
        if cold_start:
            # Chrome's startup window IS this window — asking for another
            # would open a second, unwanted one.
            return ""
        if self._cdp.is_connected:
            return (
                ""
                if self._cdp.create_window(target_url, callback)
                else "cdp-send-failed"
            )
        return self._open_window_via_cli(target_url)

    def _open_window_via_cli(self, url: str) -> str:
        """Fallback open: `chrome --new-window <url>` against the live process.

        This is the Chrome singleton working FOR us — a second invocation with
        the same --user-data-dir does not start a browser, it asks the running
        one to open a window, which is exactly what we want. That window
        inherits the running process's class, so the pin rule still applies.

        ⚠ The argv MUST still carry `--class` and the debug port. This path runs
        seconds after a cold spawn (session restore replays several saved URLs),
        so it can lose the race and become the PRIMARY process instead of
        handing off — and a primary process without `--class` puts its windows
        under the default `google-chrome` class, which the pin rule does not
        match. They would escape onto whatever workspace the user is looking at:
        the exact failure this whole mechanism exists to prevent.
        """
        executable = chrome_executable()
        if not executable:
            return "chrome-not-installed"
        argv = build_chrome_argv(
            executable, self._window_class, self._profile, self.cdp_port()
        )
        argv += ["--new-window", url]
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except OSError:
            log.exception("chrome --new-window failed")
            return "chrome-spawn-failed"
        threading.Thread(target=proc.wait, daemon=True, name="chrome-open-reap").start()
        return ""

    def close_window(self, target_id: str) -> None:
        """Close one of our windows by CDP target id."""
        if target_id:
            self._cdp.close_target(target_id)

    # -- workspace follow -----------------------------------------------

    def _apply_pin_rule(self) -> None:
        """(Re)install the rule pinning our Chrome class to our workspace.

        Records whether it actually took, so `ensure_running` can retry a pin
        that was deferred (IDE window not mapped yet) or rejected.
        """
        if not hyprland_ipc.hyprland_available():
            return
        workspace_id, workspace_name = self._watcher.resolve_now()
        if not workspace_id:
            log.info("own window not mapped yet — browser pin deferred")
            return
        target = hyprland_ipc.workspace_rule_target(workspace_id, workspace_name)
        self._pinned = hyprland_ipc.apply_keyword(
            hyprland_ipc.build_workspace_rule(target, self._window_class)
        )

    def _on_workspace_moved_thread(
        self, workspace_id: int, workspace_name: str
    ) -> None:
        """Hyprland reader thread → GUI thread. Never touch state here.

        GC is suspended across the emit per gotcha #10: queued-connection
        payload marshalling allocates, and a collection racing the Qt render
        thread mid-paint is the 3.14 SEGV. Same treatment every other
        worker-thread emit in this codebase gets.
        """
        emit_gc_safe(self._workspaceMoved, workspace_id, workspace_name)

    @Slot(int, str)
    def _on_workspace_moved(self, workspace_id: int, workspace_name: str) -> None:
        """The IDE moved workspace — re-pin so the NEXT window follows it.

        Later rules win in Hyprland, so re-issuing is enough; there is no
        remove step. Windows already open stay where they are (a rule only
        acts at map time), which is the accepted v1 behaviour.
        """
        target = hyprland_ipc.workspace_rule_target(workspace_id, workspace_name)
        hyprland_ipc.apply_keyword(
            hyprland_ipc.build_workspace_rule(target, self._window_class)
        )

    def workspace_label(self) -> str:
        """Human-readable workspace for the notification toast; "" if unknown.

        Reads the watcher's CACHE. This runs on the GUI thread on every
        Ctrl+Shift+B / globe click, and `resolve_now` shells out to `hyprctl`
        with a 2s timeout — a stall the user would feel as the whole IDE
        freezing. The cache is kept current by the event watcher; only fall
        back to a live resolve when nothing has been cached yet.
        """
        if self._watcher.workspace_id:
            return self._watcher.workspace_name or str(self._watcher.workspace_id)
        workspace_id, workspace_name = self._watcher.resolve_now()
        return workspace_name or str(workspace_id) if workspace_id else ""
